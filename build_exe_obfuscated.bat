@echo off
rem build_exe_obfuscated.bat - ShinyBot OFUSCADO con PyArmor + PyInstaller (Windows).
rem
rem ANTES DE EMPAQUETAR: rellena en pxg_bot/license.py
rem   _BUILTIN_SERVER = "https://lic.tudominio.com"
rem   _BUILTIN_SECRET = "<el mismo LICENSE_SECRET del servidor>"
rem Asi el server y el secreto quedan dentro del binario ofuscado (no en config.json).
rem
rem Requiere:  python -m pip install pyarmor pyinstaller
rem Uso:       build_exe_obfuscated.bat
setlocal
cd /d "%~dp0"

python -m pip show pyarmor >nul 2>nul
if errorlevel 1 (
  echo [*] Instalando PyArmor...
  python -m pip install pyarmor
)
python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 python -m pip install pyinstaller

echo [*] Ofuscando pxg_bot con PyArmor...
rmdir /s /q build\obf 2>nul
pyarmor gen -O build\obf --recursive pxg_bot
if errorlevel 1 goto err

set COMMON=--noconfirm --clean --onefile ^
  --paths build\obf ^
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

echo [*] Empaquetando shinybot.exe (consola, ofuscado)...
python -m PyInstaller %COMMON% --name shinybot --console main.py
if errorlevel 1 goto err

echo [*] Empaquetando shinybot-gui.exe (sin consola, ofuscado)...
python -m PyInstaller %COMMON% --name shinybot-gui --windowed main.py
if errorlevel 1 goto err

echo [+] Listo en dist\. Ofuscado con PyArmor.
goto done

:err
echo [!] Fallo el empaquetado.
exit /b 1

:done
endlocal
