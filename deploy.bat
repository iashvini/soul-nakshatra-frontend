@echo off
echo.
echo ========================================
echo   Soul Nakshatra — Deploy to Cloudflare
echo ========================================
echo.

REM Copy latest HTML to index.html
copy /Y vedic-astrology-analyzer.html index.html
echo [1/3] Copied vedic-astrology-analyzer.html to index.html

REM Stage all changes
git add index.html
echo [2/3] Staged index.html

REM Commit with timestamp
for /f "tokens=1-5 delims=/ " %%a in ("%date%") do set d=%%c-%%b-%%a
for /f "tokens=1-2 delims=: " %%a in ("%time%") do set t=%%a:%%b
git commit -m "deploy %d% %t%"
echo [3/3] Committed

REM Push to GitHub — Cloudflare auto-deploys from here
git push origin main
echo.
echo Pushed to GitHub
echo Cloudflare will auto-deploy in ~30 seconds
echo Live at: https://soul-nakshatra-frontend.pages.dev
echo.
pause
