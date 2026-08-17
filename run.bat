@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Audiobookery

REM Console output is intentionally English only. It is the first thing a user
REM sees, before the app window opens, so it must read the same for everyone
REM regardless of the interface language chosen inside the application.

set "VENV_DIR=.venv"
set "PY=%VENV_DIR%\Scripts\python.exe"
set "TORCH_INDEX=https://download.pytorch.org/whl/cu124"

echo ============================================================
echo   Audiobookery - audiobooks from your own library
echo ============================================================
echo.

if not exist "audiobookery.py" goto :no_script
if exist "%PY%" goto :env_ready

REM ---------- 1. Virtual environment ----------
REM Flow uses goto labels on purpose. Parenthesised cmd blocks break apart as
REM soon as an unescaped bracket shows up in echoed text.

echo [1/3] Creating virtual environment...
where uv >nul 2>nul
if errorlevel 1 goto :venv_stdlib

echo       Using uv.
uv venv "%VENV_DIR%" --python 3.11
if errorlevel 1 goto :err_venv
set "INSTALL=uv pip install --python %PY%"
goto :install

:venv_stdlib
echo       uv not found, falling back to the system python.
python -m venv "%VENV_DIR%"
if errorlevel 1 goto :err_venv
set "INSTALL=%PY% -m pip install"
"%PY%" -m pip install --upgrade pip

REM ---------- 2. Dependencies ----------
:install
echo.
echo [2/3] Installing PyTorch with CUDA - several GB, this takes a while...
REM The version is pinned deliberately: chatterbox-tts requires exactly 2.6.0.
REM Without the pin, pip would replace the CUDA build with the CPU one.
%INSTALL% torch==2.6.0 torchaudio==2.6.0 --index-url %TORCH_INDEX%
if not errorlevel 1 goto :install_rest

echo WARNING: the CUDA build failed to install, trying the CPU build...
%INSTALL% torch==2.6.0 torchaudio==2.6.0
if errorlevel 1 goto :err_deps

:install_rest
echo.
echo [3/3] Installing the remaining dependencies...
%INSTALL% -r requirements.txt
if errorlevel 1 goto :err_deps
echo.
echo Installation finished.
echo.
goto :check_gpu

:env_ready
echo Virtual environment found.

REM ---------- 3. GPU check ----------
:check_gpu
"%PY%" -c "import torch;print('CUDA:', torch.cuda.is_available(), '-', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no GPU')" 2>nul

REM ---------- 4. Launch ----------
echo.
echo Starting the application...
echo.
"%PY%" audiobookery.py
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" goto :err_app
goto :end

REM ---------- Error branches ----------
:no_script
echo ERROR: audiobookery.py is not in this folder.
echo        Run run.bat from the project folder itself.
pause
exit /b 1

:err_venv
echo.
echo ERROR: could not create the virtual environment.
echo        Check that Python 3.10 or newer is installed.
pause
exit /b 1

:err_deps
echo.
echo ERROR: installing dependencies failed.
echo        Delete the .venv folder and run run.bat again.
pause
exit /b 1

:err_app
echo.
echo The application exited with error code %EXITCODE%.
pause
exit /b %EXITCODE%

:end
