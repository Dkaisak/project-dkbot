@echo off
rem build_exe.bat - empaqueta el bot y la GUI con PyInstaller (Windows).
rem La DLL y el agente .lua van como datos.
rem Uso:  build_exe.bat
setlocal
cd /d "%~dp0"

python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  echo [*] Instalando PyInstaller...
  python -m pip install pyinstaller
)

set DATA=tools\pxg_agent.lua;tools --add-data "tools\agent_loader\pxg_agent_loader.dll;tools\agent_loader"

echo [*] Empaquetando main.py (bot + subcomandos)...
pyinstaller --noconfirm --name dkbot ^
  --add-data "tools\pxg_agent.lua;tools" ^
  --add-data "tools\agent_loader\pxg_agent_loader.dll;tools\agent_loader" ^
  --add-data "pxg_bot\web;pxg_bot\web" ^
  --add-data "config.json;." ^
  main.py

echo [+] Listo en dist\dkbot\. Copia config.json junto al exe y usa run_windows.bat
endlocal
