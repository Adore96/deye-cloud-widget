@echo off
rem Sets up a private Python environment on first run, then starts the widget without a console window.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: setting things up, this takes a minute...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Python 3.10 or newer is required: https://www.python.org/downloads/
        echo Tick "Add python.exe to PATH" during install, then run this file again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Installing the requirements failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
)
start "" ".venv\Scripts\pythonw.exe" app.py
