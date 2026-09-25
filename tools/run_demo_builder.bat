@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 cupx_demo.py
) else (
    python cupx_demo.py
)

if not %errorlevel%==0 (
    echo.
    echo CUPX Demo Data Builder exited with an error.
    echo Review bootstrap_error.log or the selected output\logs folder.
    pause
)
endlocal
