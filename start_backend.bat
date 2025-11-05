@echo off
echo 🌱 Activating virtual environment...
call venv\Scripts\activate

echo 🚀 Starting JPL backend with Eventlet...
python run_eventlet.py

pause
  