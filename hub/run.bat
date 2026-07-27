@echo off
REM Start the LIMS Print Hub (Windows, central desktop). Uses uv to provision the venv.
setlocal
cd /d "%~dp0"

where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

uv venv .venv
uv pip install -r requirements.txt --python .venv
echo.
.venv\Scripts\python.exe app.py
pause
