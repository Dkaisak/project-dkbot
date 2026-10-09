@echo off
rem run_windows.bat - arranca ShinyBot en Windows:
rem   1) compila la DLL del loader si falta,
rem   2) inyecta el agente en pxgme.exe,
rem   3) arranca la GUI web.
rem
rem Uso:  run_windows.bat            (inyecta + GUI)
rem       run_windows.bat --no-gui   (solo inyecta)
setlocal
cd /d "%~dp0"

set DLL=tools\agent_loader\pxg_agent_loader.dll

if not exist "%DLL%" (
  echo [*] Compilando la DLL del loader...
  call tools\agent_loader\build_windows.bat
  if errorlevel 1 goto err
)

echo [*] Inyectando el agente en pxgme.exe...
python tools\inject_windows.py --dll "%DLL%"
if errorlevel 1 echo [!] La inyeccion fallo (revisa el log). El bot puede no funcionar.

if /I "%~1"=="--no-gui" goto done

echo [*] Arrancando la app de escritorio...
python main.py app
goto done

:err
echo [!] No se pudo compilar la DLL.
exit /b 1

:done
endlocal
