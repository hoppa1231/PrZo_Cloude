@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 pzcloud_gui.py
  exit /b
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw pzcloud_gui.py
  exit /b
)
echo Не найден Python 3. Установите Python с python.org или скачайте PZCloudCoop.exe из Releases.
pause
