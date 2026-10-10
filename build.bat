@echo off
setlocal
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 goto failed
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
if errorlevel 1 goto failed
echo Build complete: dist\InvoiceExtractor\InvoiceExtractor.exe
pause
exit /b 0
:failed
echo Build failed. Check the error above.
pause
exit /b 1
