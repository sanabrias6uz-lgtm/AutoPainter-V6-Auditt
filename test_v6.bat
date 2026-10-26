@ECHO OFF
cd /d "%~dp0"
IF EXIST venv\Scripts\python.exe (
    venv\Scripts\python.exe test_v6.py
) ELSE (
    python test_v6.py
)
PAUSE
