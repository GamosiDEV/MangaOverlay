<#
.SYNOPSIS
    Desinstala o MangaOverlay (copiado para a pasta de instalação pelo install.ps1).

.DESCRIPTION
    Remove o app, os atalhos e o registro em Configurações > Aplicativos.
    As obras, traduções salvas e configurações ficam, a não ser com -Purge.

.PARAMETER Purge
    Apaga também obras, traduções, configurações, chaves de API e os modelos baixados.
.PARAMETER Quiet
    Não pergunta nada nem espera Enter no fim.
#>
[CmdletBinding()]
param([switch]$Purge, [switch]$Quiet)

$ErrorActionPreference = 'Stop'
$Prefix = $PSScriptRoot
$Py = Join-Path $Prefix '.venv\Scripts\python.exe'
if (-not (Test-Path (Join-Path $Prefix 'main.py'))) { throw 'Rode este script de dentro da pasta de instalação.' }

if (-not $Quiet -and -not $Purge) {
    $answer = Read-Host 'Apagar também as obras, traduções salvas, configurações e modelos baixados? (s/N)'
    $Purge = $answer -match '^[sSyY]'
}

Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

if ($Purge -and (Test-Path $Py)) {
    # Modelos no cache do Hugging Face e chaves de API no Gerenciador de Credenciais
    Push-Location $Prefix
    & {
        $ErrorActionPreference = 'Continue'
        & $Py -m mangaoverlay.cleanup --purge
    }
    Pop-Location
}

foreach ($folder in 'Programs', 'Desktop') {
    Remove-Item (Join-Path ([Environment]::GetFolderPath($folder)) 'MangaOverlay.lnk') -ErrorAction SilentlyContinue
}
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MangaOverlay' -Recurse -ErrorAction SilentlyContinue

Set-Location $env:TEMP  # o Windows não apaga a pasta em que o processo está
Remove-Item $Prefix -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path $Prefix) {
    Write-Host "Alguns arquivos estavam em uso; apague a pasta $Prefix manualmente." -ForegroundColor Yellow
} else {
    Write-Host 'MangaOverlay removido.' -ForegroundColor Green
}

if ($Purge) {
    # Configurações, banco de obras e traduções, cache do EasyOCR e log (pastas do platformdirs)
    Remove-Item (Join-Path $env:LOCALAPPDATA 'MangaOverlay') -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host 'Obras, traduções, configurações e modelos também foram apagados.'
} else {
    Write-Host 'Obras, traduções e configurações foram mantidas.'
}
if (-not $Quiet) { Read-Host 'Pressione Enter para fechar' | Out-Null }
