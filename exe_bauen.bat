@echo off
cd /d "%~dp0"
echo Installiere benoetigte Pakete...
python -m pip install --upgrade pynput pyinstaller sv-ttk
if errorlevel 1 (
  echo.
  echo Python wurde nicht gefunden. Installier Python von python.org
  echo und setz beim Installieren den Haken bei "Add python.exe to PATH".
  pause
  exit /b
)
echo.
echo Baue StreamMerkzettel.exe ...
set ICON=
if exist icon.ico set ICON=--icon icon.ico
set PLUGIN=
if exist com.xjanx.clipp-helper.streamDeckPlugin (
  set PLUGIN=--add-data "com.xjanx.clipp-helper.streamDeckPlugin;."
  echo Stream-Deck-Plugin gefunden - wird mit eingepackt.
)
python -m PyInstaller --onefile --windowed --noconfirm --collect-data sv_ttk %ICON% %PLUGIN% --name StreamMerkzettel stream_merkzettel.py
echo.
echo Fertig! Die exe liegt im Ordner "dist".
explorer dist
pause
