@ECHO OFF
cd /d "%~dp0"
IF EXIST venv\Scripts\python.exe (
    venv\Scripts\python.exe run_painter_strokes_v6.py %*
) ELSE (
    python run_painter_strokes_v6.py %*
)
PAUSE
