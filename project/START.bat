@echo off
REM ExamForge AI - Quick Start Script

cls
echo.
echo ========================================================================
echo.  ExamForge AI - Quick Start
echo.
echo ========================================================================
echo.

REM Check if python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.8+ from https://python.org
    pause
    exit /b 1
)

echo [1/3] Checking backend dependencies...
cd /d "%~dp0"
python -m pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install -q -r requirements.txt
)
echo OK

echo.
echo [2/3] Starting Backend Server...
echo Launching backend on http://localhost:8000
start cmd /k "cd backend && python app.py"
timeout /t 5 >nul

echo.
echo [3/3] Starting Frontend Server...
echo Launching frontend on http://localhost:3000
start cmd /k "cd "%~dp0" && python frontend_server.py"
timeout /t 3 >nul

echo.
echo ========================================================================
echo.
echo ✓ ExamForge AI is ready!
echo.
echo Frontend:  http://localhost:3000
echo Backend:   http://localhost:8000
echo Test:      http://localhost:3000/test-connection.html
echo.
echo ========================================================================
echo.
pause
