@echo off
cd /d "%~dp0"
echo Open http://127.0.0.1:4317/ in your browser.
call npm run dev
pause
