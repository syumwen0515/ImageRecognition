@echo off
cd /d "%~dp0"

echo.
echo ==========================================
echo  Bib Number Recognition System
echo ==========================================
echo.

:: Prefer Python 3.12 (required for GPU/EasyOCR support)
set PYTHON_CMD=
where py >nul 2>&1 && py -3.12 --version >nul 2>&1 && set PYTHON_CMD=py -3.12
if "%PYTHON_CMD%"=="" (
    where python3.12 >nul 2>&1 && set PYTHON_CMD=python3.12
)
if "%PYTHON_CMD%"=="" (
    where python >nul 2>&1 && set PYTHON_CMD=python
)
if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python not found. Please install Python 3.12.
    pause & exit /b 1
)

echo [Python] Using: %PYTHON_CMD%

if not exist "venv\Scripts\activate.bat" (
    echo [1/4] Creating virtual environment with Python 3.12...
    %PYTHON_CMD% -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv
        pause & exit /b 1
    )

    echo [2/4] Installing PyTorch with CUDA 12.4 support...
    venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124 -q
    if errorlevel 1 (
        echo [WARN] CUDA torch install failed, falling back to CPU torch...
    )

    echo [3/4] Installing remaining packages...
    venv\Scripts\python.exe -m pip install -r requirements.txt -q
    if errorlevel 1 (
        echo [ERROR] pip install failed
        pause & exit /b 1
    )
) else (
    echo [INFO] venv already exists, skipping install.
)

echo [4/4] Starting server...
echo.
echo   Frontend : http://localhost:8000
echo   API docs : http://localhost:8000/docs
echo.
echo   GPU acceleration: EasyOCR will auto-detect NVIDIA GPU.
echo   Press Ctrl+C to stop.
echo.

venv\Scripts\uvicorn.exe main:app --reload --host 0.0.0.0 --port 8000

pause
