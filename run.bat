@echo off
REM Launcher script for EMCD Repricer
REM Activates virtual environment and runs the application

setlocal enabledelayedexpansion

REM Define paths
set SCRIPT_DIR=%~dp0
set VENV_DIR=%SCRIPT_DIR%venv
set VENV_ACTIVATE=%VENV_DIR%\Scripts\activate.bat
set MAIN_SCRIPT=%SCRIPT_DIR%emcd_repricer.py
set CONFIG_FILE=%SCRIPT_DIR%config.yaml

REM Check if virtual environment exists
if not exist "%VENV_ACTIVATE%" (
    echo.
    echo ============================================
    echo ERROR: Virtual environment not found!
    echo.
    echo Please run install_deps.bat first
    echo ============================================
    pause
    exit /b 1
)

REM Check if main script exists
if not exist "%MAIN_SCRIPT%" (
    echo.
    echo ERROR: emcd_repricer.py not found!
    pause
    exit /b 1
)

REM Check if config exists
if not exist "%CONFIG_FILE%" (
    echo.
    echo ERROR: config.yaml not found!
    echo.
    echo Copy config.example.yaml to config.yaml and edit it with your settings
    pause
    exit /b 1
)

echo.
echo ============================================
echo Starting EMCD Repricer...
echo ============================================
echo.

REM Activate virtual environment
call "%VENV_ACTIVATE%"

REM Run script from root directory
python emcd_repricer.py --config config.yaml

REM Show pause if script exits with error
if errorlevel 1 (
    echo.
    echo Press any key to exit...
    pause
)
