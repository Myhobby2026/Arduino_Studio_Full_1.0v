@echo off
REM ===========================================================================
REM  Arduino Studio - build a standalone Windows executable
REM
REM  Double-click this file (or run it from a terminal in this folder) to get
REM  dist\Arduino Studio\Arduino Studio.exe - a folder you can copy to any
REM  Windows 10/11 machine. No Python installation is required there, but the
REM  target machine still needs arduino-cli on PATH (or its path set in
REM  Arduino Studio > Settings), because the app shells out to it.
REM
REM  What it does:
REM    1. finds Python (py -3.13 ... py -3.9, then python on PATH)
REM    2. creates .venv in this folder if it does not exist yet
REM    3. installs requirements-dev.txt (app deps + PyInstaller)
REM    4. generates arduino_studio\resources\arduino.ico
REM    5. runs PyInstaller with arduino_studio.spec
REM ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PYTHON="
for %%V in (3.13 3.12 3.11 3.10 3.9) do (
    if not defined PYTHON (
        py -%%V -c "import sys,tkinter" >nul 2>nul && set "PYTHON=py -%%V"
    )
)
if not defined PYTHON (
    python -c "import sys,tkinter" >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo.
    echo ERROR: no usable Python found. Install Python 3.9 or newer from
    echo        https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    echo        Tkinter must be included ^(it is, in the python.org installer^).
    echo.
    pause
    exit /b 1
)

echo.
echo --- using: %PYTHON%
%PYTHON% --version

if not exist ".venv\Scripts\python.exe" (
    echo --- creating .venv
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
)
set "VENV_PY=.venv\Scripts\python.exe"

echo --- installing dependencies
"%VENV_PY%" -m pip install --upgrade pip >nul
"%VENV_PY%" -m pip install -r requirements-dev.txt
if errorlevel 1 (
    echo ERROR: dependency installation failed - check your network / proxy settings.
    pause
    exit /b 1
)

echo --- generating the application icon
"%VENV_PY%" tools\make_icon.py >nul 2>nul
if not exist "arduino_studio\resources\arduino.ico" (
    echo     note: no icon generated, the build will use the default one.
)

echo --- running PyInstaller
"%VENV_PY%" -m PyInstaller --noconfirm --clean arduino_studio.spec
if errorlevel 1 (
    echo ERROR: the build failed. The messages above say which step broke.
    pause
    exit /b 1
)

echo.
echo ===========================================================================
echo  Done. The app is in:
echo      %CD%\dist\Arduino Studio\Arduino Studio.exe
echo.
echo  Copy the whole "Arduino Studio" folder to the target machine ^(not just the
echo  exe^). Start it with a double-click; the first run asks for the arduino-cli
echo  location if it is not on PATH.
echo ===========================================================================
echo.
pause
endlocal
