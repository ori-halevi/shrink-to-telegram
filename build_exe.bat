@echo off
rem Builds a single portable ShrinkToTelegram.exe (needs: pip install pyinstaller)
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name ShrinkToTelegram --icon "assets\icon.ico" ^
  --add-data "assets;assets" --version-file version_info.txt ^
  shrink_to_telegram.py
echo.
echo Done: dist\ShrinkToTelegram.exe
pause
