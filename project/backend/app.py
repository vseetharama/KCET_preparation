"""
ExamForge AI — Backend Server
FastAPI-based backend for question generation
"""

import warnings
import logging
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', message='.*tqdm.*')
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', message='.*IProgress.*')
warnings.filterwarnings('ignore', category=DeprecationWarning)

# Suppress verbose HTTP connection logging
logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)
logging.getLogger('sentence_transformers').setLevel(logging.WARNING)
logging.getLogger('transformers').setLevel(logging.WARNING)

import re, json, uuid, io, os, sys, time, random
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import cv2
import fitz
import faiss
import pytesseract
from PIL import Image
from docx import Document as DocxDocument
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
from sentence_transformers import SentenceTransformer
import uvicorn

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Load embedder
print('🔄 Loading embedder model...')
os.environ['SENTENCE_TRANSFORMERS_HOME'] = os.path.expanduser('~/.cache/sentence-transformers')

embedder = None
for attempt in range(3):
    try:
        embedder = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
        print('✓ Embedder loaded successfully')
        break
    except Exception as e:
        if attempt < 2:
            print(f'⚠️ Network attempt {attempt + 1} failed, retrying in 2s...')
            time.sleep(2)
        else:
            print(f'❌ Failed to load embedder after 3 attempts')
            print(f'   Error: {type(e).__name__}')
            raise

# ─────────────────────────────────────────────────────────────────────────────
# Vector Store for RAG
# ─────────────────────────────────────────────────────────────────────────────

class VectorStore:
    def __init__(self):
        self.index = None
        self.chunks = []
        self.raw_text = ""
        self.dim = 384
    
    def reset(self):
        self.index = faiss.IndexFlatL2(self.dim)
        self.chunks = []
        self.raw_text = ""
    
    def add(self, texts):
        if self.index is None:
            self.reset()
        vecs = embedder.encode(texts, show_progress_bar=False).astype('float32')
        self.index.add(vecs)
        self.chunks.extend(texts)
    
    def search(self, query, k=20):
        if not self.chunks:
            return []
        vec = embedder.encode([query]).astype('float32')
        k = min(k, len(self.chunks))
        _, ids = self.index.search(vec, k)
        return [self.chunks[i] for i in ids[0] if i < len(self.chunks)]

store = VectorStore()

# ─────────────────────────────────────────────────────────────────────────────
# Text Extraction & Processing
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_for_ocr(img):
    try:
        img_np = np.array(img.convert('RGB'))
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        gray = cv2.fastNlMeansDenoising(gray, h=10)
        thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)
        return Image.fromarray(thresh)
    except Exception as e:
        print(f'OCR preprocessing failed: {e}')
        return img

def extract_text_from_pdf(file_bytes):
    doc = fitz.open(stream=file_bytes, filetype='pdf')
    pages = []
    for page in doc:
        text = page.get_text().strip()
        if len(text) > 50:
            pages.append(text)
        else:
            try:
                pix = page.get_pixmap(dpi=400)
                img = Image.open(io.BytesIO(pix.tobytes('png')))
                img = preprocess_for_ocr(img)
                try:
                    ocr_text = pytesseract.image_to_string(img, lang='eng', config='--psm 6 --oem 3')
                    if ocr_text.strip():
                        pages.append(ocr_text)
                except:
                    pass
            except Exception as e:
                print(f'PDF page processing failed: {e}')
    return '\n'.join(pages)

def extract_text_from_docx(file_bytes):
    doc = DocxDocument(io.BytesIO(file_bytes))
    return '\n'.join(p.text for p in doc.paragraphs if p.text.strip())

def extract_text_from_txt(file_bytes):
    return file_bytes.decode('utf-8', errors='ignore')

def chunk_text(text, size=400, overlap=80):
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(' '.join(words[i:i+size]))
        i += size - overlap
    return [c for c in chunks if len(c.strip()) > 30]

# ─────────────────────────────────────────────────────────────────────────────
# LLM Question Generation
# ─────────────────────────────────────────────────────────────────────────────

def parse_llm_json(raw):
    """Robust JSON parsing with multiple fallback strategies"""
    original = raw
    raw = raw.strip()
    
    # Remove markdown code blocks
    raw = re.sub(r'^```(?:json|)[\n\r]*', '', raw)
    raw = re.sub(r'[\n\r]*```$', '', raw)
    raw = raw.strip()
    
    # Try direct JSON parse first
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and 'questions' in data:
            return data.get('questions', [])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError as e:
        print(f'  ⚠️ Direct JSON parse failed at position {e.pos}: {str(e)[:60]}')
    
    # Try to find JSON array in the response
    try:
        match = re.search(r'\[\s*\{.*?\}\s*\]', raw, re.DOTALL)
        if match:
            json_str = match.group()
            data = json.loads(json_str)
            if isinstance(data, list):
                return data
    except:
        pass
    
    # Try to extract individual JSON objects and build array
    try:
        objects = re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw)
        if objects:
            questions = []
            for obj_str in objects:
                try:
                    obj = json.loads(obj_str)
                    if isinstance(obj, dict) and 'q' in obj:
                        questions.append(obj)
                except:
                    pass
            if questions:
                print(f'  ✅ Extracted {len(questions)} objects from raw response')
                return questions
    except:
        pass
    
    print(f'  ❌ All parsing strategies failed')
    print(f'  Raw preview: {original[:300]}...')
    return []

def validate_question(q):
    """Check if question has all required fields"""
    required = {'q', 'opts', 'ans'}
    if not isinstance(q, dict):
        return False
    if not all(k in q for k in required):
        return False
    if not isinstance(q.get('opts'), list) or len(q.get('opts', [])) != 4:
        return False
    if not isinstance(q.get('ans'), int) or not (0 <= q['ans'] <= 3):
        return False
    return True

def extract_key_concepts(text):
    """Extract key terms and concepts from text"""
    # Split into sentences handling standard punctuation and newlines
    sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
    concepts = []
    
    for sent in sentences:
        sent = sent.strip()
        if len(sent) > 20:
            # Extract capitalized terms (likely important)
            terms = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', sent)
            concepts.extend(terms)
            
            # Also extract common noun phrases and technical terms
            # Find words of 4+ characters that could be domain-specific
            words = re.findall(r'\b[a-z]{4,}\b', sent.lower())
            concepts.extend([w.capitalize() for w in words if w not in ['that', 'this', 'with', 'from', 'have', 'been', 'also', 'said']])
    
    return list(set(concepts))

def create_distractor(correct_answer, all_terms, used_distractors=None):
    """Create a plausible but incorrect option from available concepts"""
    if used_distractors is None:
        used_distractors = set()
    
    # Exclude correct answer and already used distractors
    candidates = [t for t in all_terms 
                 if t != correct_answer 
                 and t not in used_distractors
                 and len(t) > 2
                 and len(t) < len(correct_answer) * 2]  # Keep similar length
    
    if candidates:
        # Prefer terms of similar length or closely related
        candidates.sort(key=lambda x: abs(len(x) - len(correct_answer)))
        return candidates[0]
    
    # Return None if no suitable distractor found (question will be skipped)
    return None

def extract_existing_mcqs(text, subject):
    """Attempt to extract existing MCQ questions directly from raw document text"""
    questions = []
    
    # Split text into potential question blocks based on question numbering
    blocks = re.split(r'\n\s*(?:Q?\d+[\.\)])\s*', '\n' + text)
    
    for block in blocks[1:]:
        block = block.strip()
        if not block:
            continue
            
        parts = re.split(r'(?:^|\s+)(?:\([A-Da-d1-4]\)|[A-Da-d1-4][\.\)])\s+', block)
        
        if len(parts) >= 5: # 1 question + at least 4 options
            q_text = parts[0].strip()
            
            if len(q_text) < 10 or len(q_text) > 1000:
                continue
                
            opts = [p.strip() for p in parts[1:5]]
            opts = [o.split('\n')[0].strip() for o in opts]
            
            if all(len(o) > 0 for o in opts):
                q = {
                    'id': f'extracted-{uuid.uuid4().hex[:6]}',
                    'q': q_text,
                    'opts': opts,
                    'ans': 0, # Default to 0 since we don't have answer key
                    'type': 'MCQ',
                    'topic': subject,
                    'marks': 1
                }
                # Prevent duplicates
                if not any(eq['q'] == q_text for eq in questions):
                    questions.append(q)
                
    return questions

def generate_mcq_questions_local(context_chunks, subject, used_questions=None, prompt=None):
    """Generate MCQ questions from text chunks WITHOUT API calls"""
    if not context_chunks:
        return []
    
    if used_questions is None:
        used_questions = set()

    questions = []
    current_run_questions = set()
    all_text = ' '.join(context_chunks)
    
    # Better sentence splitting (handle newlines and standard punctuation)
    sentences = re.split(r'(?<=[.!?])\s+|\n+', all_text)
    
    # Filter out sentences that look like existing MCQ options or are garbled
    clean_sentences = []
    for s in sentences:
        s = s.strip()
        # Remove leading numbers like "1. ", "45. " to clean up the question stem
        s = re.sub(r'^\d+\.\s*', '', s)
        
        # Skip if it starts with option-like characters e.g., (A), A), (1)
        if re.search(r'^\s*(\([A-Da-d1-4]\)|[A-Da-d1-4]\))', s):
            continue
            
        # Skip if it has multiple options embedded (garbled PDF text)
        if len(re.findall(r'\([A-Da-d1-4]\)', s)) >= 2:
            continue
            
        # Skip if it looks like an existing question
        if s.endswith('?') or re.search(r'^\s*Q\d+', s, re.IGNORECASE) or re.search(r'^\s*(choose|select)\s', s, re.IGNORECASE) or "your answer:" in s.lower() or "marks:" in s.lower():
            continue
            
        if 20 <= len(s) <= 250:
            clean_sentences.append(s)
            
    # Shuffle sentences so we extract different questions for different sets
    random.shuffle(clean_sentences)

    # Extract all concepts from chunks
    all_concepts = []
    for chunk in context_chunks:
        all_concepts.extend(extract_key_concepts(chunk))
    
    # Remove artificial limit to get a rich variety of distractors
    all_concepts = list(set([c for c in all_concepts if len(c) > 3]))
    
    # Generate questions from sentences
    for i, sentence in enumerate(clean_sentences):
        if len(questions) >= 20:
            break
        
        # Find a concept in this sentence to use as answer
        # Sort concepts by length (descending) to match larger phrases first
        concepts_in_sent = [c for c in sorted(all_concepts, key=len, reverse=True) if c.lower() in sentence.lower()]
        if not concepts_in_sent:
            continue
        
        correct_answer = concepts_in_sent[0]
        
        # Avoid creating blanks for common grammatical words if capitalized
        if correct_answer.lower() in ['the', 'this', 'that', 'they', 'what', 'which', 'how', 'when', 'why']:
            continue
            
        # Create question by extracting the sentence and removing the key term
        question_text = sentence.replace(correct_answer, "___________").strip()
        if question_text.endswith('_'):
            question_text = question_text[:-3] + "?"
        if not question_text.endswith('?'):
            question_text += "?"
        
        # Create 3 distractor options from available concepts only
        distractors = []
        used_distractors = set()
        attempts = 0
        max_attempts = len(all_concepts)  # Try with available concepts
        
        while len(distractors) < 3 and attempts < max_attempts:
            distractor = create_distractor(correct_answer, all_concepts, used_distractors)
            if distractor is None:
                # No more suitable distractors available - skip this question
                break
            if distractor not in distractors and distractor != correct_answer:
                distractors.append(distractor)
                used_distractors.add(distractor)
            attempts += 1
        
        # Only create question if we have all 3 distractors (no fallback options)
        if len(distractors) == 3:
            # Create options (shuffle them)
            options = [correct_answer] + distractors
            correct_idx = 0
            
            # Create the MCQ
            q = {
                'id': f'local-{uuid.uuid4().hex[:6]}',
                'q': question_text,
                'opts': options,
                'ans': correct_idx,
                'type': 'MCQ',
                'topic': subject,
                'marks': 1
            }
            
            if validate_question(q):
                questions.append(q)
                current_run_questions.add(question_text)
    
    print(f'✅ Generated {len(questions)} MCQ questions locally')
    if len(questions) < 10:
        print(f'   ⚠️ WARNING: Generated fewer than 10 questions. Consider:')
        print(f'      - Uploading documents with more diverse content')
        print(f'      - Using documents with clear technical terminology')
        print(f'      - Ensuring sufficient concepts can be extracted')
    return questions

def generate_mcq_set(context_chunks, subject, set_label, used_questions):
    """Generate UNIQUE MCQ set from uploaded paper content"""
    if not context_chunks:
        print(f'❌ ERROR: No content chunks available for Set {set_label}')
        return []
    
    context = '\n\n'.join(context_chunks[:20])
    num_previously_used = len(used_questions)
    used_str = '\n'.join(f'- {q}' for q in list(used_questions)[:100]) if used_questions else 'None'

    prompt = f"""You are a question paper generation expert. Your ONLY job is to generate EXACTLY 20 UNIQUE multiple choice questions for {subject}.

CRITICAL RULES:
1. Use ONLY content from the uploaded exam papers provided below
2. Do NOT repeat any of these previously used questions
3. Each question MUST be DIFFERENT and UNIQUE
4. Generate questions from DIFFERENT parts of the content
5. Vary the difficulty and topics covered

UPLOADED EXAM PAPER CONTENT:
---
{context}
---

DO NOT USE THESE {num_previously_used} PREVIOUSLY GENERATED QUESTIONS:
{used_str}

MUST GENERATE 20 COMPLETELY NEW QUESTIONS BASED ONLY ON THE CONTENT ABOVE.

FORMAT - Return ONLY valid JSON array, NO other text:
[
  {{"q":"Unique Question 1?","opts":["Option A","Option B","Option C","Option D"],"ans":0,"topic":"Topic","type":"MCQ","marks":1}},
  {{"q":"Different Question 2?","opts":["Option A","Option B","Option C","Option D"],"ans":1,"topic":"Topic","type":"MCQ","marks":1}}
]

CONSTRAINTS:
- EXACTLY 20 questions in the array
- Each "q" MUST be completely different from all {num_previously_used} previously used questions
- Each question MUST have exactly 4 options
- "ans" MUST be 0, 1, 2, or 3
- NO markdown, NO explanation, NO text before/after JSON
- Base questions ONLY on uploaded content, NOTHING ELSE

Generate 20 NEW questions now:"""

    try:
        print(f'📍 Generating Set {set_label} from {len(context_chunks)} content chunks...')
        # LOCAL MCQ GENERATION - Implemented in Phase 2
        questions = generate_mcq_questions_local(context_chunks, subject, used_questions, prompt)
        print(f'   Generated: {len(questions)} questions locally')
        
        # Validate questions
        valid_questions = [q for q in questions if validate_question(q)]
        print(f'   Valid: {len(valid_questions)}/{len(questions)} questions')
        
        if not valid_questions:
            print(f'❌ FAILED: No valid questions could be generated for Set {set_label}')
            return []
        
        # Check for duplicates
        unique_new_questions = []
        for q in valid_questions:
            if q.get('q', '') not in used_questions:
                unique_new_questions.append(q)
                used_questions.add(q.get('q', ''))
        
        if len(unique_new_questions) < len(valid_questions):
            print(f'   ⚠️ Warning: {len(valid_questions) - len(unique_new_questions)} duplicates removed')
        
        # Add metadata
        final_questions = []
        for i, q in enumerate(unique_new_questions[:20]):
            q_copy = q.copy()
            q_copy['id'] = f"{set_label}-{i}"
            q_copy['type'] = q_copy.get('type', 'MCQ')
            q_copy['marks'] = q_copy.get('marks', 1)
            final_questions.append(q_copy)
        
        print(f'✅ Set {set_label}: {len(final_questions)} questions ready')
        return final_questions
    
    except Exception as e:
        print(f'❌ ERROR generating Set {set_label}: {type(e).__name__}: {str(e)[:200]}')
        import traceback
        traceback.print_exc()
        return []

SUBJECT_KEYWORDS = {
    'Mathematics': ['equation', 'algebra', 'calculus', 'trigonometry', 'derivative', 'integral', 'polynomial', 'matrix'],
    'Physics': ['velocity', 'force', 'energy', 'momentum', 'gravity', 'acceleration', 'kinematics', 'optics'],
    'Chemistry': ['element', 'compound', 'molecule', 'reaction', 'oxidation', 'valence', 'isotope', 'bond'],
    'Biology': ['cell', 'dna', 'protein', 'organism', 'photosynthesis', 'mitochondria', 'enzyme', 'mitosis'],
    'History': ['war', 'emperor', 'civilization', 'dynasty', 'treaty', 'revolution', 'century', 'conquest'],
    'Literature': ['novel', 'poetry', 'author', 'character', 'theme', 'metaphor', 'protagonist', 'narrative'],
    'Geography': ['continent', 'ocean', 'climate', 'latitude', 'longitude', 'terrain', 'region', 'country'],
    'Economics': ['market', 'supply', 'demand', 'price', 'inflation', 'gdp', 'currency', 'profit'],
}

def detect_subject_local(chunks):
    """Detect subject from content chunks using keyword matching"""
    if not chunks:
        return 'General Subject'
    
    # Combine sample chunks
    text = ' '.join(chunks[:10]).lower()
    
    # Count keyword matches
    scores = {}
    for subject, keywords in SUBJECT_KEYWORDS.items():
        score = sum(text.count(kw) for kw in keywords)
        if score > 0:
            scores[subject] = score
    
    if scores:
        detected = max(scores, key=scores.get)
        print(f'✓ Subject detected: {detected}')
        return detected
    
    # Default to General if no match
    print(f'⚠️ No specific subject detected, using General Subject')
    return 'General Subject'

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Setup
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title='ExamForge Backend', version='1.0.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# ─────────────────────────────────────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get('/health')
def health():
    """Health check endpoint"""
    return {'status': 'ok', 'chunks_indexed': len(store.chunks)}

@app.get('/debug')
def debug():
    """Debug endpoint - shows indexed content preview"""
    return {'chunks_indexed': len(store.chunks), 'sample': store.chunks[:2]}

@app.post('/upload')
async def upload(files: List[UploadFile] = File(...)):
    """Upload and index exam papers"""
    if len(files) > 10:
        raise HTTPException(400, 'Maximum 10 files allowed')
    
    store.reset()
    doc_ids, total_chunks = [], 0
    
    for f in files:
        content = await f.read()
        name = f.filename.lower()
        
        # Extract text based on file type
        if name.endswith('.pdf'):
            text = extract_text_from_pdf(content)
        elif name.endswith('.docx'):
            text = extract_text_from_docx(content)
        elif name.endswith(('.txt', '.doc')):
            text = extract_text_from_txt(content)
        else:
            continue
        
        chunks = chunk_text(text)
        store.add(chunks)
        store.raw_text += text + "\n\n"
        total_chunks += len(chunks)
        doc_ids.append(str(uuid.uuid4()))
        print(f'✓ Indexed {f.filename}: {len(chunks)} chunks')
    
    return {
        'success': True,
        'doc_ids': doc_ids,
        'total_chunks': total_chunks,
        'message': f'{len(doc_ids)} files indexed with {total_chunks} chunks'
    }

class GenerateRequest(BaseModel):
    difficulty: str = 'medium'
    count: int = 20
    types: list = ['MCQ']
    subject: str = 'General Subject'
    num_sets: int = 4

@app.post('/generate')
def generate(req: GenerateRequest):
    """Generate question sets from indexed documents"""
    if not store.chunks:
        raise HTTPException(400, 'No documents uploaded yet.')
    
    # Detect subject from content
    subject = req.subject
    if subject == 'General Subject':
        # LOCAL SUBJECT DETECTION - Implemented in Phase 3
        subject = detect_subject_local(store.chunks)
    
    print(f'Generating for subject: {subject}')
    
    # FIRST: Try to extract existing questions from raw text
    extracted = extract_existing_mcqs(store.raw_text, subject)
    if extracted and len(extracted) >= 5:
        print(f"✅ Extracted {len(extracted)} existing questions directly from the document!")
        import random
        random.shuffle(extracted)
        
        sets = []
        for i in range(req.num_sets):
            start_idx = i * 20
            end_idx = start_idx + 20
            
            if start_idx >= len(extracted):
                set_qs = extracted[:]
                random.shuffle(set_qs)
                set_qs = set_qs[:20]
            else:
                set_qs = extracted[start_idx:end_idx]
                if len(set_qs) < 20 and len(extracted) >= 20:
                    pad = [q for q in extracted if q not in set_qs]
                    random.shuffle(pad)
                    set_qs.extend(pad[:20 - len(set_qs)])
            
            final_qs = []
            label = ['A', 'B', 'C', 'D'][i % 4]
            for j, q in enumerate(set_qs):
                q_copy = q.copy()
                q_copy['id'] = f"{label}-{j}"
                final_qs.append(q_copy)
                
            sets.append(final_qs)
            print(f'Set {label}: {len(final_qs)} extracted questions')
            
        return {'sets': sets}
        
    print("⚠️ Could not extract enough existing questions. Falling back to Generator.")
    
    used_questions = set()
    sets = []
    
    # Filter chunks to prioritize the requested subject
    subject_chunks = []
    if subject in SUBJECT_KEYWORDS:
        kws = SUBJECT_KEYWORDS[subject]
        subject_chunks = [c for c in store.chunks if any(kw in c.lower() for kw in kws)]
    
    for label in ['A', 'B', 'C', 'D']:
        # Retrieve more chunks to ensure enough content for 80 unique questions (4 sets * 20 qs)
        chunks = store.search(f'{subject} multiple choice questions', k=100)
        
        # If we found subject-specific chunks, filter the search results
        if subject_chunks:
            filtered = [c for c in chunks if c in subject_chunks]
            if len(filtered) >= 5: # Only use if we have a reasonable amount
                chunks = filtered
                
        questions = generate_mcq_set(chunks, subject, label, used_questions)
        sets.append(questions)
        print(f'Set {label}: {len(questions)} questions')
    
    return {'sets': sets}

class AnalyzeRequest(BaseModel):
    questions: list
    answers: dict
    student: dict = {}

@app.post('/analyze')
def analyze(req: AnalyzeRequest):
    """Analyze student exam performance"""
    total, earned = 0, 0
    topic_scores, type_scores, results = {}, {}, []
    
    for i, q in enumerate(req.questions):
        m = 1
        total += m
        topic = q.get('topic', 'General')
        topic_scores.setdefault(topic, {'earned': 0, 'total': 0})['total'] += m
        type_scores.setdefault('MCQ', {'earned': 0, 'total': 0})['total'] += m
        
        given = req.answers.get(str(i))
        e, status = 0, 'wrong'
        
        if given is None or given == '':
            status = 'unanswered'
        elif str(given) == str(q.get('ans')):
            e, status = m, 'correct'
        
        earned += e
        topic_scores[topic]['earned'] += e
        type_scores['MCQ']['earned'] += e
        
        results.append({
            'q': q.get('q'),
            'type': 'MCQ',
            'topic': topic,
            'given': given,
            'correctAns': q.get('ans'),
            'earned': e,
            'marks': m,
            'status': status
        })
    
    pct = round((earned / total) * 100) if total else 0
    strong, can_improve, weak = [], [], []
    
    for t, s in topic_scores.items():
        p = round((s['earned'] / s['total']) * 100) if s['total'] else 0
        if p >= 70:
            strong.append({'topic': t, 'pct': p})
        elif p >= 40:
            can_improve.append({'topic': t, 'pct': p})
        else:
            weak.append({'topic': t, 'pct': p})
    
    rec = 'Excellent! ' if pct >= 75 else 'Good effort. ' if pct >= 50 else 'Needs improvement. '
    if weak:
        rec += f"Focus on: {', '.join(w['topic'] for w in weak)}."
    
    return {
        'percentage': pct,
        'earned': earned,
        'total': total,
        'topicScores': topic_scores,
        'typeScores': type_scores,
        'strong': strong,
        'canImprove': can_improve,
        'weak': weak,
        'questionResults': results,
        'pass': pct >= 40,
        'recommendation': rec
    }

# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('\n' + '='*60)
    print('🚀 ExamForge Backend Starting...')
    print('='*60 + '\n')
    
    uvicorn.run(
        app,
        host='127.0.0.1',
        port=8000,
        log_level='info',
        access_log=True
    )
