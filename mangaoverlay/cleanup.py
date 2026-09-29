"""Limpeza feita pelos desinstaladores antes de apagar a pasta do app: `python -m mangaoverlay.cleanup [--purge]`.

Sempre remove os atalhos de teclado que o app criou no GNOME. Com --purge, apaga também os modelos baixados
e as chaves de API do chaveiro. As pastas de dados, configuração e cache ficam a cargo do desinstalador.
"""

import shutil
import sys

from .platform_info import IS_LINUX


def _step(name: str, action) -> None:
    try:
        action()
    except Exception as exc:  # um passo que falha não impede os outros
        print(f"Não foi possível {name}: {exc}", file=sys.stderr)


def _remove_gnome_shortcuts() -> None:
    from .hotkeys import ACTIONS, GnomeShortcut

    for action, (_arg, label) in ACTIONS.items():
        GnomeShortcut(action, label).uninstall()


def _remove_keys() -> None:
    from . import credentials

    for name in (credentials.OPENAI, credentials.ANTHROPIC):
        credentials.set_key(name, "")


def main(argv: list[str]) -> int:
    if IS_LINUX and shutil.which("gsettings"):
        _step("remover os atalhos do GNOME", _remove_gnome_shortcuts)
    if "--purge" in argv:
        from .models import delete_all

        _step("apagar os modelos baixados", delete_all)
        _step("apagar as chaves de API do chaveiro", _remove_keys)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
