@echo off
setlocal

REM Move to this script's directory so relative paths always work.
cd /d "%~dp0"

set "VENV_DIR=venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_ACTIVATE=%VENV_DIR%\Scripts\activate.bat"
set "PROJECT_DIR=%CD%"
set "STAMP_FILE=%VENV_DIR%\.deps_installed"

if not exist "%VENV_PYTHON%" (
    echo [INFO] Virtual environment not found. Creating one...
    py -3 -m venv "%VENV_DIR%" 2>nul
    if errorlevel 1 (
        python -m venv "%VENV_DIR%"
    )
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

if /I not "%VIRTUAL_ENV%"=="%PROJECT_DIR%\%VENV_DIR%" (
    call "%VENV_ACTIVATE%"
    if errorlevel 1 (
        echo [ERROR] Failed to activate virtual environment.
        pause
        exit /b 1
    )
)

if not exist "%STAMP_FILE%" (
    echo [INFO] First-time setup: installing dependencies...
    python -m pip install --upgrade pip
    if errorlevel 1 (
        echo [ERROR] pip upgrade failed.
        pause
        exit /b 1
    )

    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Installing requirements failed.
        pause
        exit /b 1
    )

    python -m playwright install chromium
    if errorlevel 1 (
        echo [ERROR] Playwright Chromium install failed.
        pause
        exit /b 1
    )

    type nul > "%STAMP_FILE%"
)

echo [INFO] Starting UI dashboard...
python -m streamlit run ui_app.py

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [WARN] UI process ended with exit code %EXIT_CODE%.
    pause
)

endlocal & exit /b %EXIT_CODE%