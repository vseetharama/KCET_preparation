#!/usr/bin/env python
"""Simple HTTP Server for Frontend — Connects to Backend at http://localhost:8000"""

import http.server
import socketserver
import os
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

PORT = 3000
# Get the directory of this script and construct the path to frontend
SCRIPT_DIR = Path(__file__).parent
FRONTEND_DIR = str(SCRIPT_DIR / 'frontend')

class FrontendHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)
    
    def end_headers(self):
        # Add CORS headers to allow communication with backend
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        super().end_headers()
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()
    
    def log_message(self, format, *args):
        # Better logging
        if '200 GET' in format or '404' in format:
            print(f'  {self.client_address[0]} - {format % args}')

if __name__ == '__main__':
    os.chdir(FRONTEND_DIR)
    
    # Allow address reuse to prevent socket errors on quick restarts
    socketserver.TCPServer.allow_reuse_address = True
    
    # Try to find an available port starting from 3000
    httpd = None
    while PORT < 3010:
        try:
            httpd = socketserver.TCPServer(("", PORT), FrontendHandler)
            break
        except OSError as e:
            print(f"⚠️ Port {PORT} is already in use. Trying port {PORT + 1}...")
            PORT += 1
            
    if not httpd:
        print("❌ Could not find an open port between 3000 and 3009.")
        exit(1)

    print("=" * 70)
    print("🚀 ExamForge Frontend Server")
    print("=" * 70)
    print(f"\n✓ Frontend Directory: {FRONTEND_DIR}")
    print(f"✓ Server URL: http://localhost:{PORT}")
    print(f"✓ Backend URL: http://localhost:8000")
    print(f"\nOpen browser and go to: http://localhost:{PORT}")
    print("\nPress Ctrl+C to stop server\n")
    
    print(f"Listening on http://localhost:{PORT}...\n")
    try:
        with httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\n✓ Server stopped")
