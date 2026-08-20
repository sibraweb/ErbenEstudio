@echo off
title ERBEN ESTUDIO
cd /d "%~dp0"

where py >nul 2>&1 && (py arrancar.py & goto :eof)
where python >nul 2>&1 && (python arrancar.py & goto :eof)

echo.
echo   No encuentro Python en esta maquina.
echo   Instalalo desde python.org y volve a abrir este acceso.
echo.
pause
