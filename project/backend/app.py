# ===================== ADD BELOW IMPORTS =====================
# Authentication System (NEW - PHASE 1)

students_db = {}
sessions = {}

# ===================== REQUEST MODELS =====================

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class SubmitExamRequest(BaseModel):
    student_id: str
    questions: list
    answers: dict


# ===================== AUTH APIs =====================

@app.post('/register')
def register(req: RegisterRequest):
    # check existing email
    for s in students_db.values():
        if s["email"] == req.email:
            return {"error": "Email already registered"}
    
    student_id = "KCET" + str(uuid.uuid4())[:8]

    students_db[student_id] = {
        "name": req.name,
        "email": req.email,
        "password": req.password,
        "scores": []
    }

    return {
        "student_id": student_id,
        "message": "Registered successfully"
    }


@app.post('/login')
def login(req: LoginRequest):
    for sid, s in students_db.items():
        if s["email"] == req.email and s["password"] == req.password:
            sessions[sid] = True
            return {"student_id": sid}
    
    return {"error": "Invalid credentials"}


def is_logged_in(student_id):
    return student_id in sessions


# ===================== SUBMIT EXAM =====================

@app.post('/submit_exam')
def submit_exam(req: SubmitExamRequest):
    if req.student_id not in students_db:
        return {"error": "Invalid student"}

    # reuse existing analyze logic
    analysis = analyze(AnalyzeRequest(
        questions=req.questions,
        answers=req.answers
    ))

    score = analysis["percentage"]

    students_db[req.student_id]["scores"].append(score)

    return {
        "message": "Exam submitted",
        "analysis": analysis
    }