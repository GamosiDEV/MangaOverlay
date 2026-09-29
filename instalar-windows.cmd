@echo off
rem Instalador do MangaOverlay para Windows: basta dar dois cliques neste arquivo.
rem Opcoes (repassadas ao install.ps1): -Cpu, -NoModels, -NoStart, -NoDesktopShortcut
title Instalando o MangaOverlay
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" echo A instalacao falhou (codigo %CODE%). Veja as mensagens acima.
if not defined MANGAOVERLAY_NO_PAUSE pause
exit /b %CODE%
