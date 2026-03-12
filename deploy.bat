@echo off
cd C:\LAPTOP_FILES_ALL\Modal
git add index.html
git commit -m "deploy %date% %time%"
git push
echo Deploy complete.
