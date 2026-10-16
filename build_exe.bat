@echo off
REM Rebuild VideosHubDownloader.exe from source.
REM Requires: Python 3.12 with tkinter, plus `pip install pyinstaller av`.
setlocal
cd /d "%~dp0"

set PY=pythonw
where python >nul 2>nul || set PY=python

echo [*] Cleaning previous build ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

echo [*] Building ...
%PY% -m PyInstaller --noconfirm --clean ^
  --name "VideosHubDownloader" ^
  --onefile --windowed ^
  --collect-all av ^
  --hidden-import av ^
  --hidden-import videoshub_core ^
  videoshub_gui.py

if errorlevel 1 (
  echo [!] Build FAILED.
  exit /b 1
)

echo.
echo [*] Done -^> dist\VideosHubDownloader.exe
dir /b dist
endlocal
