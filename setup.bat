@echo off
REM Installation script for EMCD Repricer
REM Creates virtual environment and installs dependencies

setlocal enabledelayedexpansion

REM Define paths
set SCRIPT_DIR=%~dp0
set VENV_DIR=%SCRIPT_DIR%venv
set REQUIREMENTS_FILE=%SCRIPT_DIR%requirements.txt

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ============================================
    echo ERROR: Python not found!
    echo.
    echo Please install Python from: https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation
    echo.
    echo Then run this script again
    echo ============================================
    pause
    exit /b 1
)

if not exist "%REQUIREMENTS_FILE%" (
    echo.
    echo ERROR: requirements.txt not found at: %REQUIREMENTS_FILE%
    pause
    exit /b 1
)

echo.
echo ============================================
echo EMCD Repricer - Setting Up Environment
echo ============================================
echo.

REM Create virtual environment
if not exist "%VENV_DIR%" (
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
)

REM Activate venv and install dependencies
echo.
echo Activating virtual environment...
call "%VENV_DIR%\Scripts\activate.bat"

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r "%REQUIREMENTS_FILE%"

echo.
echo Installing Playwright browsers...
python -m playwright install chromium

if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies or Playwright browsers
    pause
    exit /b 1
)

echo.
echo ============================================
echo Setup complete!
echo ============================================
pause
