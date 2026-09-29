#!/usr/bin/env bash
# Desinstala o MangaOverlay (copiado para a pasta de instalação pelo install.sh).
#
#   ./uninstall.sh          remove o app, o comando, o atalho e os atalhos de teclado do GNOME
#   ./uninstall.sh --purge  remove também obras, traduções salvas, configurações, chaves de API e modelos baixados

set -euo pipefail

PREFIX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PURGE=0
[[ "${1:-}" == --purge ]] && PURGE=1

[[ -f "$PREFIX/main.py" && -d "$PREFIX/.venv" ]] || { echo "Rode este script de dentro da pasta de instalação." >&2; exit 1; }

pkill -f "$PREFIX/main.py" 2>/dev/null || true

# Atalhos de teclado do GNOME; com --purge, também os modelos baixados e as chaves de API do chaveiro
cleanup_args=()
((PURGE)) && cleanup_args+=(--purge)
(cd "$PREFIX" && .venv/bin/python -m mangaoverlay.cleanup "${cleanup_args[@]}") || true

rm -f "$HOME/.local/bin/mangaoverlay" "$DATA/applications/mangaoverlay.desktop"
rm -rf "$PREFIX"
echo "MangaOverlay removido."

if ((PURGE)); then
    rm -rf "$DATA/MangaOverlay" "${XDG_CONFIG_HOME:-$HOME/.config}/MangaOverlay" \
        "${XDG_CACHE_HOME:-$HOME/.cache}/MangaOverlay" "${XDG_STATE_HOME:-$HOME/.local/state}/MangaOverlay"
    echo "Obras, traduções, configurações e modelos também foram apagados."
else
    echo "Obras, traduções e configurações foram mantidas (use --purge para apagá-las)."
fi
