@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" /B ".venv\Scripts\pythonw.exe" -m engine.cli gui
) else (
    start "" /B ".venv\Scripts\python.exe" -m engine.cli gui
)
