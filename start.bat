@echo off
echo ========================================
echo  YouTube Video Creator - Starting...
echo ========================================

:: Check if venv exists
if not exist "venv" (
  echo Creating virtual environment...
  python -m venv venv
)

:: Activate venv
call venv\Scripts\activate

:: Install dependencies
echo Installing dependencies...
pip install -r requirements.txt --quiet

:: Check .env
if not exist ".env" (
  echo.
  echo [WARNING] .env file not found!
  echo Copying .env.example to .env - please fill in your API keys.
  copy .env.example .env
  echo.
  pause
)

:: Launch
echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
