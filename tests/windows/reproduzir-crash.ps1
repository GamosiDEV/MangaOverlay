<#
.SYNOPSIS
    Reproduz o crash do MangaOverlay no Windows (0xC0000374) e mostra o log de cada rodada.

.DESCRIPTION
    Abre a página de teste no Paint, abre o app e aperta Ctrl+Alt+M algumas vezes, com uma pausa entre elas.
    O crash depende do tempo: pausas de 30 s ou mais (a thread ociosa do Qt expira em 30 s) reproduziam o erro
    em quase todas as rodadas. Ver docs/WINDOWS-CRASH.md.

    Rode de dentro da pasta do repositório, com o app já instalado pelo instalar-windows.cmd
    (ou aponte -Python para o python.exe de um ambiente próprio, com -Main para o main.py).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tests\windows\reproduzir-crash.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tests\windows\reproduzir-crash.ps1 -Rodadas 5 -Pausa 45
#>
param(
    [int]$Rodadas = 3,
    [int]$Traducoes = 3,
    [int]$Pausa = 30,
    [int]$Aquecimento = 40,
    [string]$Prefix = (Join-Path $env:LOCALAPPDATA 'Programs\MangaOverlay'),
    [string]$Python = '',
    [string]$Main = ''
)

$ErrorActionPreference = 'Continue'
if (-not $Python) { $Python = Join-Path $Prefix '.venv\Scripts\python.exe' }
if (-not $Main) { $Main = Join-Path $Prefix 'main.py' }
$Pythonw = Join-Path (Split-Path $Python) 'pythonw.exe'
$Log = Join-Path $env:LOCALAPPDATA 'MangaOverlay\Logs\mangaoverlay.log'
$Pagina = (Resolve-Path (Join-Path $PSScriptRoot '..\fixtures\pagina-sintetica.png')).Path
$Tecla = "import time; from pynput.keyboard import Controller, Key; k = Controller(); k.press(Key.ctrl); k.press(Key.alt); k.press('m'); time.sleep(0.1); k.release('m'); k.release(Key.alt); k.release(Key.ctrl)"

foreach ($path in $Python, $Pythonw, $Main) {
    if (-not (Test-Path $path)) { throw "Não encontrado: $path" }
}
Write-Host 'Feche o MangaOverlay se estiver aberto (o script abre o dele).' -ForegroundColor Yellow
$paint = Start-Process mspaint.exe "`"$Pagina`"" -PassThru
Start-Sleep -Seconds 3

$crashes = 0
foreach ($rodada in 1..$Rodadas) {
    # O log é do app instalado e acumula entre execuções: mostra só o que esta rodada acrescentar
    $inicio = if (Test-Path $Log) { @(Get-Content $Log -Encoding UTF8).Count } else { 0 }
    $app = Start-Process $Pythonw -ArgumentList "`"$Main`"" -WorkingDirectory (Split-Path $Main) -PassThru
    Start-Sleep -Seconds $Aquecimento
    foreach ($n in 1..$Traducoes) {
        if ($app.HasExited) { break }
        # Traz o Paint para a frente e aperta o atalho, como o usuário faria
        (New-Object -ComObject WScript.Shell).AppActivate($paint.Id) | Out-Null
        & $Python -c $Tecla
        Start-Sleep -Seconds $Pausa
    }
    $codigo = if ($app.HasExited) { '0x{0:X}' -f $app.ExitCode } else { '-' }
    $cor = if ($app.HasExited) { 'Red' } else { 'Green' }
    Write-Host "=== rodada ${rodada}: fechou sozinho = $($app.HasExited), código = $codigo ===" -ForegroundColor $cor
    if ($app.HasExited) { $crashes++ } else { Stop-Process -Id $app.Id -Force }
    if (Test-Path $Log) {
        Get-Content $Log -Encoding UTF8 | Select-Object -Skip $inicio |
            Select-String -NotMatch 'HF_TOKEN|pin_memory|super\(\).__init__|max_new_tokens'
    }
}
Stop-Process -Id $paint.Id -ErrorAction SilentlyContinue
Write-Host ''
if ($crashes) {
    Write-Host "$crashes de $Rodadas rodada(s) com crash." -ForegroundColor Red
    exit 1
}
Write-Host "Nenhum crash em $Rodadas rodada(s)." -ForegroundColor Green
