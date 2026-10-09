@echo off
rem build_exe.bat - empaqueta ShinyBot con PyInstaller (Windows).
rem   dist\shinybot.exe      -> consola (CLI + app de escritorio / GUI web)
rem   dist\shinybot-gui.exe  -> sin consola (doble clic -> app de escritorio)
rem Incluye la interfaz web (pxg_bot/web), el agente .lua, la DLL, el icono y
rem config.json. Empaqueta pywebview/pystray/Pillow para la ventana nativa.
rem Uso:  build_exe.bat
setlocal
cd /d "%~dp0"

python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  echo [*] Instalando PyInstaller...
  python -m pip install pyinstaller
)

set COMMON=--noconfirm --clean --onefile ^
  --collect-all webview ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import pystray._win32 ^
  --icon tools/icon.ico ^
  --add-data "pxg_bot/web;pxg_bot/web" ^
  --add-data "tools/pxg_agent.lua;tools" ^
  --add-data "tools/agent_loader/pxg_agent_loader.dll;tools/agent_loader" ^
  --add-data "tools/icon.ico;tools" ^
  --add-data "tools/icon.png;tools" ^
  --add-data "config.json;."

echo [*] Empaquetando shinybot.exe (consola)...
python -m PyInstaller %COMMON% --name shinybot --console main.py
if errorlevel 1 goto err

echo [*] Empaquetando shinybot-gui.exe (sin consola)...
python -m PyInstaller %COMMON% --name shinybot-gui --windowed main.py
if errorlevel 1 goto err

echo [+] Listo en dist\. Ejecuta dist\shinybot-gui.exe (app de escritorio).
goto done

:err
echo [!] Fallo el empaquetado.
exit /b 1

:done
endlocal
