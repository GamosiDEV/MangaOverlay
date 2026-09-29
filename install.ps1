<#
.SYNOPSIS
    Instalador do MangaOverlay para Windows.

.DESCRIPTION
    Instala tudo para o usuário atual, sem precisar de Python no sistema: o uv baixa um Python próprio,
    o PyTorch certo para a placa de vídeo, as dependências e os modelos, e cria os atalhos no menu Iniciar
    e na área de trabalho. O app aparece em Configurações > Aplicativos, de onde pode ser desinstalado.
    Só pede permissão de administrador se faltar o Microsoft Visual C++ Redistributable.

    Rodar de novo atualiza a instalação (as obras, traduções e configurações não são tocadas).
    O jeito mais simples de rodar é dar dois cliques em instalar-windows.cmd.

.PARAMETER Cpu
    Instala o PyTorch sem CUDA (sem placa NVIDIA, ou para economizar ~3 GB).
.PARAMETER Cuda
    Força a variante do PyTorch: cu126 (GPUs antigas), cu128 ou cu130. Padrão: automático.
.PARAMETER NoModels
    Não baixa os modelos agora (são baixados no primeiro uso, ~3,6 GB).
.PARAMETER NoStart
    Não abre o app no fim.
.PARAMETER NoDesktopShortcut
    Não cria o atalho na área de trabalho.
.PARAMETER Prefix
    Onde instalar. Padrão: %LOCALAPPDATA%\Programs\MangaOverlay
#>
[CmdletBinding()]
param(
    [switch]$Cpu,
    [ValidateSet('auto', 'cu126', 'cu128', 'cu130')][string]$Cuda = 'auto',
    [switch]$NoModels,
    [switch]$NoStart,
    [switch]$NoDesktopShortcut,
    [string]$Prefix = (Join-Path $env:LOCALAPPDATA 'Programs\MangaOverlay')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # a barra de progresso do Invoke-WebRequest deixa o download muito lento
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$PythonVersion = '3.12'
$TorchSpec = @('torch==2.11.*', 'torchvision==0.26.*')
$Src = $PSScriptRoot

function Step([string]$Message) { Write-Host ''; Write-Host "==> $Message" -ForegroundColor Cyan }
function Warn([string]$Message) { Write-Host "Aviso: $Message" -ForegroundColor Yellow }

# Roda um programa e para se ele falhar. O uv escreve o progresso no stderr, que o PowerShell 5.1
# transformaria em erro com ErrorActionPreference=Stop; aqui só o código de saída conta.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Falhou (código $LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

# Roda um programa só para saber se deu certo (código de saída), sem mostrar a saída
function Test-Native([string]$Exe, [string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    & $Exe @Arguments 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-Download([string]$Url, [string]$OutFile) {
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
}

if (-not [Environment]::Is64BitOperatingSystem) { throw 'O MangaOverlay precisa do Windows de 64 bits.' }
if (-not ((Test-Path (Join-Path $Src 'main.py')) -and (Test-Path (Join-Path $Src 'mangaoverlay')))) {
    throw 'Rode o install.ps1 de dentro da pasta do MangaOverlay.'
}
$Version = ([regex]'__version__ = "([^"]+)"').Match((Get-Content (Join-Path $Src 'mangaoverlay\__init__.py') -Raw)).Groups[1].Value

# --- 1. Visual C++ Redistributable ---------------------------------------------------------
# O PyTorch precisa das bibliotecas do Visual C++ (msvcp140.dll e companhia), que nem todo Windows tem.
$System32 = [Environment]::GetFolderPath('System')
if (-not ((Test-Path (Join-Path $System32 'msvcp140.dll')) -and (Test-Path (Join-Path $System32 'vcruntime140_1.dll')))) {
    Step 'Instalando o Microsoft Visual C++ Redistributable (o Windows vai pedir permissão)'
    $vcredist = Join-Path $env:TEMP 'vc_redist.x64.exe'
    Get-Download 'https://aka.ms/vs/17/release/vc_redist.x64.exe' $vcredist
    try {
        $process = Start-Process $vcredist -ArgumentList '/install', '/quiet', '/norestart' -Verb RunAs -Wait -PassThru
        $code = $process.ExitCode
    } catch {
        $code = 'permissão negada'
    }
    Remove-Item $vcredist -ErrorAction SilentlyContinue
    if ($code -notin 0, 1638, 3010) {
        Warn "o Visual C++ Redistributable não foi instalado ($code). Se o app não abrir, instale-o de https://aka.ms/vs/17/release/vc_redist.x64.exe"
    }
}

# --- 2. Arquivos do app --------------------------------------------------------------------
Step "Copiando o app para $Prefix"
# Atualização: fecha a versão em execução, que prende os arquivos do ambiente Python
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
$Prefix = (Resolve-Path $Prefix).Path
if ($Prefix -ne (Resolve-Path $Src).Path) {
    foreach ($dir in 'mangaoverlay', 'assets') {
        $target = Join-Path $Prefix $dir
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }  # sem restos de uma versão anterior
        Copy-Item (Join-Path $Src $dir) $target -Recurse
    }
    foreach ($file in 'main.py', 'requirements.txt', 'constraints.txt', 'LICENSE', 'README.md') {
        Copy-Item (Join-Path $Src $file) $Prefix -Force
    }
    Copy-Item (Join-Path $Src 'installer\uninstall.ps1') (Join-Path $Prefix 'uninstall.ps1') -Force
    Get-ChildItem (Join-Path $Prefix 'mangaoverlay') -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
}

# --- 3. uv e Python ------------------------------------------------------------------------
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Prefix 'python'
$env:UV_CACHE_DIR = Join-Path $Prefix '.uv-cache'
$Tools = Join-Path $Prefix 'tools'
$Uv = Join-Path $Tools 'uv.exe'
if (-not (Test-Path $Uv)) {
    Step 'Baixando o uv (gerenciador de Python)'
    $zip = Join-Path $env:TEMP "uv-$([guid]::NewGuid()).zip"
    Get-Download 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' $zip
    $unpacked = Join-Path $env:TEMP "uv-$([guid]::NewGuid())"
    Expand-Archive $zip -DestinationPath $unpacked -Force
    New-Item -ItemType Directory -Force -Path $Tools | Out-Null
    Get-ChildItem $unpacked -Recurse -Filter '*.exe' | Copy-Item -Destination $Tools -Force
    Remove-Item $zip, $unpacked -Recurse -Force
    if (-not (Test-Path $Uv)) { throw "O uv não foi instalado em $Tools." }
}

$Venv = Join-Path $Prefix '.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$Pyw = Join-Path $Venv 'Scripts\pythonw.exe'
$versionCheck = "import sys; sys.exit(sys.version_info[:2] != ($($PythonVersion.Replace('.', ', '))))"
if (-not ((Test-Path $Py) -and (Test-Native $Py @('-c', $versionCheck)))) {
    Step "Criando o ambiente Python $PythonVersion"
    if (Test-Path $Venv) { Remove-Item $Venv -Recurse -Force }
    Invoke-Native $Uv @('venv', '--python', $PythonVersion, '--managed-python', $Venv)
}

# --- 4. PyTorch ----------------------------------------------------------------------------
$variant = if ($Cpu) { 'cpu' } else { $Cuda }
if ($variant -eq 'auto') {
    $variant = 'cpu'
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    $smiPath = if ($smi) { $smi.Source } else { Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe' }
    if (Test-Path $smiPath) {
        $cap = & {
            $ErrorActionPreference = 'Continue'
            & $smiPath --query-gpu=compute_cap --format=csv,noheader 2>&1 | Select-Object -First 1
        }
        if ($cap -match '^\s*(\d+)\.(\d+)\s*$') {
            # O build cu128 não inclui GPUs anteriores à série GTX 16xx/RTX 20xx (compute capability 7.5)
            $variant = if ([int]$Matches[1] * 10 + [int]$Matches[2] -ge 75) { 'cu128' } else { 'cu126' }
            Write-Host "Placa NVIDIA encontrada (compute capability $($cap.Trim()))."
        }
    }
}
Step "Instalando o PyTorch ($variant)"
if ($variant -ne 'cpu') { Write-Host 'São cerca de 3 GB; pode demorar.' }
Invoke-Native $Uv (@('pip', 'install', '--python', $Py) + $TorchSpec + @('--index-url', "https://download.pytorch.org/whl/$variant"))

# --- 5. Dependências -----------------------------------------------------------------------
Step 'Instalando as dependências'
Invoke-Native $Uv @('pip', 'install', '--python', $Py, '-r', (Join-Path $Prefix 'requirements.txt'), '-c', (Join-Path $Prefix 'constraints.txt'))

if ($variant -ne 'cpu') {
    if (-not (Test-Native $Py @('-c', 'import sys, torch; sys.exit(not torch.cuda.is_available())'))) {
        Warn 'o PyTorch não conseguiu usar a placa de vídeo (driver da NVIDIA antigo ou ausente?). O app vai rodar na CPU, mais devagar.'
    }
}

# --- 6. Modelos ----------------------------------------------------------------------------
if (-not $NoModels) {
    Step 'Baixando os modelos (~3,6 GB, só desta vez)'
    Push-Location $Prefix
    try { Invoke-Native $Py @('main.py', '--download-models') }
    catch { Warn 'alguns modelos serão baixados no primeiro uso.' }
    finally { Pop-Location }
}

# --- 7. Atalhos e registro em Aplicativos ---------------------------------------------------
Step 'Criando os atalhos'
$Main = Join-Path $Prefix 'main.py'
$Icon = Join-Path $Prefix 'assets\mangaoverlay.ico'
$shell = New-Object -ComObject WScript.Shell
function New-AppShortcut([string]$Path) {
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $Pyw  # pythonw: sem janela de console
    $link.Arguments = "`"$Main`""
    $link.WorkingDirectory = $Prefix
    $link.IconLocation = "$Icon,0"
    $link.Description = 'Traduz os balões de mangá na tela'
    $link.Save()
}
New-AppShortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'MangaOverlay.lnk')
if (-not $NoDesktopShortcut) {
    New-AppShortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'MangaOverlay.lnk')
}

$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MangaOverlay'
New-Item -Path $UninstallKey -Force | Out-Null
$uninstaller = Join-Path $Prefix 'uninstall.ps1'
$sizeKb = [int]((Get-ChildItem $Prefix -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1KB)
$values = @{
    DisplayName     = 'MangaOverlay'
    DisplayVersion  = $Version
    Publisher       = 'MangaOverlay'
    DisplayIcon     = $Icon
    InstallLocation = $Prefix
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstaller`""
}
foreach ($name in $values.Keys) { Set-ItemProperty -Path $UninstallKey -Name $name -Value $values[$name] }
foreach ($name in 'NoModify', 'NoRepair') { Set-ItemProperty -Path $UninstallKey -Name $name -Value 1 -Type DWord }
Set-ItemProperty -Path $UninstallKey -Name EstimatedSize -Value $sizeKb -Type DWord

if (Test-Path $env:UV_CACHE_DIR) { Remove-Item $env:UV_CACHE_DIR -Recurse -Force }  # os pacotes já estão no ambiente

Write-Host ''
Write-Host 'MangaOverlay instalado.' -ForegroundColor Green
Write-Host '  Abrir:        pelo menu Iniciar ou pelo atalho na área de trabalho'
Write-Host '  Desinstalar:  Configurações > Aplicativos > MangaOverlay'
if (-not $NoStart) {
    Start-Process -FilePath $Pyw -ArgumentList "`"$Main`"" -WorkingDirectory $Prefix
    Write-Host '  O app foi aberto e está na bandeja (perto do relógio; pode estar nos ícones ocultos).'
}
