@echo off
rem build_exe.bat - empaqueta dkbot con PyInstaller (Windows).
rem   dist\dkbot.exe      -> consola (CLI + GUI web)
rem   dist\dkbot-gui.exe  -> sin consola (doble clic -> GUI web)
rem Incluye la interfaz web (pxg_bot/web), el agente .lua, la DLL y config.json.
rem Uso:  build_exe.bat
setlocal
cd /d "%~dp0"

python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  echo [*] Instalando PyInstaller...
  python -m pip install pyinstaller
)

echo [*] Empaquetando dkbot.exe (consola)...
python -m PyInstaller --noconfirm --clean --onefile --name dkbot --console ^
  --add-data "pxg_bot/web;pxg_bot/web" ^
  --add-data "tools/pxg_agent.lua;tools" ^
  --add-data "tools/agent_loader/pxg_agent_loader.dll;tools/agent_loader" ^
  --add-data "config.json;." ^
  main.py
if errorlevel 1 goto err

echo [*] Empaquetando dkbot-gui.exe (sin consola)...
python -m PyInstaller --noconfirm --clean --onefile --name dkbot-gui --windowed ^
  --add-data "pxg_bot/web;pxg_bot/web" ^
  --add-data "tools/pxg_agent.lua;tools" ^
  --add-data "tools/agent_loader/pxg_agent_loader.dll;tools/agent_loader" ^
  --add-data "config.json;." ^
  main.py
if errorlevel 1 goto err

echo [+] Listo en dist\. Ejecuta dist\dkbot-gui.exe (o dist\dkbot.exe gui).
goto done

:err
echo [!] Fallo el empaquetado.
exit /b 1

:done
endlocal
