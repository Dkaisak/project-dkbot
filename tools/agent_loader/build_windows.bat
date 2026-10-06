@echo off
rem Compila pxg_agent_loader.dll en Windows.
rem  - Si hay MinGW (gcc/mingw32-gcc/x86_64-w64-mingw32-gcc) lo usa.
rem  - Si no, intenta MSVC (cl).
rem  - Si no, intenta Zig (python -m ziglang cc).
rem Uso:  build_windows.bat
setlocal
cd /d "%~dp0"

set OUT=pxg_agent_loader.dll

where x86_64-w64-mingw32-gcc >nul 2>nul && set CC=x86_64-w64-mingw32-gcc
if not defined CC ( where gcc >nul 2>nul && set CC=gcc )
if not defined CC ( where mingw32-gcc >nul 2>nul && set CC=mingw32-gcc )

if defined CC (
  echo [*] Compilando con %CC%
  %CC% -shared -O2 -Wall -o %OUT% pxg_agent_loader.c -lkernel32
  goto done
)

where cl >nul 2>nul
if %errorlevel%==0 (
  echo [*] Compilando con MSVC cl
  cl /nologo /O2 /LD /Fe:%OUT% pxg_agent_loader.c kernel32.lib
  goto done
)

echo [*] Intentando Zig (python -m ziglang)
python -m ziglang cc -target x86_64-windows-gnu -shared -O2 -o %OUT% pxg_agent_loader.c
if %errorlevel%==0 goto done

echo [!] No se encontro compilador (gcc/mingw32/cl/zig). Instala MinGW-w64 o Zig.
exit /b 1

:done
if exist %OUT% (
  echo [+] Listo: %CD%\%OUT%
) else (
  echo [!] Fallo la compilacion
  exit /b 1
)
endlocal
