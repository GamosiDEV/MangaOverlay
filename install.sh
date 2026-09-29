#!/usr/bin/env bash
# Instalador do MangaOverlay para Linux.
#
# Instala tudo para o usuário atual, sem precisar de Python no sistema: o uv baixa um Python próprio,
# o PyTorch certo para a placa de vídeo, as dependências e os modelos, e cria o comando `mangaoverlay`
# e o atalho no menu de aplicativos. Só pede a senha (sudo) se faltar alguma biblioteca do sistema.
#
# Rodar de novo atualiza a instalação (as obras, traduções e configurações não são tocadas).

set -euo pipefail

# Tudo entre chaves: o bash lê o script inteiro antes de começar, então atualizar a pasta
# (git pull, por exemplo) durante a instalação não embaralha a execução.
{

PYTHON_VERSION=3.12
TORCH_SPEC=(torch==2.11.* torchvision==0.26.*)

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/MangaOverlay/app"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
TORCH_VARIANT=auto
DOWNLOAD_MODELS=1
START=1

usage() {
    cat <<EOF
Uso: ./install.sh [opções]

  --cpu            instala o PyTorch sem CUDA (sem placa NVIDIA, ou para economizar ~3 GB)
  --cuda VARIANTE  força a variante do PyTorch: cu126 (GPUs antigas), cu128 ou cu130
  --no-models      não baixa os modelos agora (são baixados no primeiro uso, ~3,6 GB)
  --no-start       não abre o app no fim
  --prefix PASTA   onde instalar (padrão: $PREFIX)
  -h, --help       mostra esta ajuda
EOF
}

while (($#)); do
    case "$1" in
        --cpu) TORCH_VARIANT=cpu ;;
        --cuda) TORCH_VARIANT="${2:?--cuda precisa de uma variante (cu126, cu128 ou cu130)}"; shift ;;
        --no-models) DOWNLOAD_MODELS=0 ;;
        --no-start) START=0 ;;
        --prefix) PREFIX="${2:?--prefix precisa de uma pasta}"; shift ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Opção desconhecida: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mAviso:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mErro:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || die "este instalador é para Linux. No Windows, use instalar-windows.cmd."
[[ -f "$SRC/main.py" && -d "$SRC/mangaoverlay" ]] || die "rode o install.sh de dentro da pasta do MangaOverlay."
command -v curl >/dev/null || command -v wget >/dev/null || die "instale o curl ou o wget e rode de novo."

download() {  # download URL -> stdout
    if command -v curl >/dev/null; then curl -fsSL "$1"; else wget -qO- "$1"; fi
}

# --- 1. Bibliotecas do sistema --------------------------------------------------------------
# O PySide6 traz o Qt, mas o plugin xcb (usado para a tradução ficar por cima das outras janelas)
# depende destas bibliotecas do sistema. A libxcb-cursor0 é a que costuma faltar.
step "Verificando as bibliotecas do sistema"
declare -A APT_PACKAGES=(
    [libxcb-cursor.so.0]=libxcb-cursor0
    [libxcb-icccm.so.4]=libxcb-icccm4
    [libxcb-keysyms.so.1]=libxcb-keysyms1
    [libxcb-image.so.0]=libxcb-image0
    [libxcb-render-util.so.0]=libxcb-render-util0
    [libxcb-shape.so.0]=libxcb-shape0
    [libxkbcommon-x11.so.0]=libxkbcommon-x11-0
    [libEGL.so.1]=libegl1
    [libfontconfig.so.1]=libfontconfig1
    [libdbus-1.so.3]=libdbus-1-3
)
declare -A DNF_PACKAGES=(
    [libxcb-cursor.so.0]=xcb-util-cursor [libxcb-icccm.so.4]=xcb-util-wm [libxcb-keysyms.so.1]=xcb-util-keysyms
    [libxcb-image.so.0]=xcb-util-image [libxcb-render-util.so.0]=xcb-util-renderutil [libxcb-shape.so.0]=libxcb
    [libxkbcommon-x11.so.0]=libxkbcommon-x11 [libEGL.so.1]=libglvnd-egl [libfontconfig.so.1]=fontconfig
    [libdbus-1.so.3]=dbus-libs
)
declare -A PACMAN_PACKAGES=(
    [libxcb-cursor.so.0]=xcb-util-cursor [libxcb-icccm.so.4]=xcb-util-wm [libxcb-keysyms.so.1]=xcb-util-keysyms
    [libxcb-image.so.0]=xcb-util-image [libxcb-render-util.so.0]=xcb-util-renderutil [libxcb-shape.so.0]=libxcb
    [libxkbcommon-x11.so.0]=libxkbcommon-x11 [libEGL.so.1]=libglvnd [libfontconfig.so.1]=fontconfig
    [libdbus-1.so.3]=dbus
)
has_library() { ldconfig -p 2>/dev/null | grep -q "$1" || compgen -G "/usr/lib*/$1" >/dev/null || compgen -G "/usr/lib/*/$1" >/dev/null; }
missing=()
for lib in "${!APT_PACKAGES[@]}"; do
    has_library "$lib" || missing+=("$lib")
done
if ((${#missing[@]})); then
    packages=()
    if command -v apt-get >/dev/null; then
        for lib in "${missing[@]}"; do packages+=("${APT_PACKAGES[$lib]}"); done
        # Lista de pacotes desatualizada (máquina recém-instalada) faz o install falhar: atualiza e tenta de novo
        install_cmd=(sh -c 'apt-get install -y "$@" || { apt-get update && apt-get install -y "$@"; }' apt "${packages[@]}")
    elif command -v dnf >/dev/null; then
        for lib in "${missing[@]}"; do packages+=("${DNF_PACKAGES[$lib]}"); done
        install_cmd=(dnf install -y "${packages[@]}")
    elif command -v pacman >/dev/null; then
        for lib in "${missing[@]}"; do packages+=("${PACMAN_PACKAGES[$lib]}"); done
        install_cmd=(pacman -S --needed --noconfirm "${packages[@]}")
    fi
    if [[ -n "${install_cmd[*]:-}" ]]; then
        echo "Faltam: ${missing[*]}"
        echo "Instalando os pacotes (pode pedir a senha): ${packages[*]}"
        if [[ $EUID -eq 0 ]]; then "${install_cmd[@]}" || warn "não foi possível instalar ${packages[*]}."
        elif command -v sudo >/dev/null; then sudo "${install_cmd[@]}" || warn "não foi possível instalar ${packages[*]}."
        else warn "instale como administrador: ${install_cmd[*]}"
        fi
    else
        warn "faltam as bibliotecas ${missing[*]}; instale-as pelo gerenciador de pacotes da sua distribuição."
    fi
else
    echo "Tudo presente."
fi

# --- 2. Arquivos do app ---------------------------------------------------------------------
step "Copiando o app para $PREFIX"
pkill -f "$PREFIX/main.py" 2>/dev/null && sleep 1 || true  # atualização: fecha a versão em execução
mkdir -p "$PREFIX"
if [[ "$(cd "$PREFIX" && pwd)" != "$SRC" ]]; then
    rm -rf "$PREFIX/mangaoverlay"  # sem restos de uma versão anterior
    cp -r "$SRC/mangaoverlay" "$SRC/assets" "$PREFIX/"
    cp "$SRC/main.py" "$SRC/requirements.txt" "$SRC/constraints.txt" "$SRC/LICENSE" "$SRC/README.md" "$PREFIX/"
    cp "$SRC/installer/uninstall.sh" "$PREFIX/uninstall.sh"
    chmod +x "$PREFIX/uninstall.sh"
    find "$PREFIX/mangaoverlay" -name __pycache__ -type d -prune -exec rm -rf {} +
fi

# --- 3. uv e Python -------------------------------------------------------------------------
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_CACHE_DIR="$PREFIX/.uv-cache"
UV="$PREFIX/tools/uv"
if [[ ! -x "$UV" ]]; then
    step "Baixando o uv (gerenciador de Python)"
    download https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$PREFIX/tools" UV_NO_MODIFY_PATH=1 sh
fi
[[ -x "$UV" ]] || die "o uv não foi instalado em $PREFIX/tools."

VENV="$PREFIX/.venv"
PY="$VENV/bin/python"
if [[ ! -x "$PY" ]] || ! "$PY" -c "import sys; sys.exit(sys.version_info[:2] != tuple(map(int, '$PYTHON_VERSION'.split('.'))))" 2>/dev/null; then
    step "Criando o ambiente Python $PYTHON_VERSION"
    rm -rf "$VENV"
    "$UV" venv --python "$PYTHON_VERSION" --managed-python "$VENV"
fi

# --- 4. PyTorch -----------------------------------------------------------------------------
if [[ "$TORCH_VARIANT" == auto ]]; then
    TORCH_VARIANT=cpu
    if command -v nvidia-smi >/dev/null; then
        cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ')"
        if [[ "$cap" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
            # O build cu128 não inclui GPUs anteriores à série GTX 16xx/RTX 20xx (compute capability 7.5)
            if ((BASH_REMATCH[1] * 10 + BASH_REMATCH[2] >= 75)); then TORCH_VARIANT=cu128; else TORCH_VARIANT=cu126; fi
            echo "Placa NVIDIA encontrada (compute capability $cap)."
        fi
    fi
fi
step "Instalando o PyTorch ($TORCH_VARIANT)"
if [[ "$TORCH_VARIANT" != cpu ]]; then echo "São cerca de 3 GB; pode demorar."; fi
"$UV" pip install --python "$PY" "${TORCH_SPEC[@]}" --index-url "https://download.pytorch.org/whl/$TORCH_VARIANT"

# --- 5. Dependências ------------------------------------------------------------------------
step "Instalando as dependências"
"$UV" pip install --python "$PY" -r "$PREFIX/requirements.txt" -c "$PREFIX/constraints.txt"

if [[ "$TORCH_VARIANT" != cpu ]] && ! "$PY" -c "import torch, sys; sys.exit(not torch.cuda.is_available())"; then
    warn "o PyTorch não conseguiu usar a placa de vídeo (driver da NVIDIA antigo ou ausente?). O app vai rodar na CPU, mais devagar."
fi

# --- 6. Modelos -----------------------------------------------------------------------------
if ((DOWNLOAD_MODELS)); then
    step "Baixando os modelos (~3,6 GB, só desta vez)"
    (cd "$PREFIX" && "$PY" main.py --download-models) || warn "alguns modelos serão baixados no primeiro uso."
fi

# --- 7. Comando e atalho --------------------------------------------------------------------
step "Criando o comando mangaoverlay e o atalho no menu"
mkdir -p "$BIN_DIR" "$APPS_DIR"
cat >"$BIN_DIR/mangaoverlay" <<EOF
#!/bin/sh
exec "$PY" "$PREFIX/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/mangaoverlay"
cat >"$APPS_DIR/mangaoverlay.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MangaOverlay
Comment=Traduz os balões de mangá na tela
Exec="$BIN_DIR/mangaoverlay"
Icon=$PREFIX/assets/mangaoverlay.png
Terminal=false
Categories=Utility;Graphics;
Keywords=manga;manhwa;tradução;OCR;
StartupNotify=false
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true

rm -rf "$UV_CACHE_DIR"  # os pacotes já estão no ambiente; o cache só ocuparia espaço

echo
printf '\033[1;32mMangaOverlay instalado.\033[0m\n'
echo "  Abrir:        pelo menu de aplicativos, ou com o comando: mangaoverlay"
echo "  Desinstalar:  $PREFIX/uninstall.sh"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "  ($BIN_DIR não está no PATH; abra um terminal novo ou use $BIN_DIR/mangaoverlay)" ;;
esac
if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]] && ! gnome-extensions list --enabled 2>/dev/null | grep -qi appindicator; then
    echo "  No GNOME, o ícone da bandeja precisa da extensão 'AppIndicator and KStatusNotifierItem Support'."
fi

if ((START)) && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    nohup "$BIN_DIR/mangaoverlay" >/dev/null 2>&1 &
    echo "  O app foi aberto e está na bandeja."
fi

exit 0
}
