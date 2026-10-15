@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe app.py --open-browser
if errorlevel 1 goto fail
goto :eof
:fail
echo Start oder Einrichtung fehlgeschlagen. Bitte die Fehlermeldung oben pruefen.
pause
