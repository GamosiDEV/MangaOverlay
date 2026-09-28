"""Linha de comando.

Mantido leve de propósito: quando o app já está rodando, `--translate`, `--colorize` e `--hide` só enviam
um comando pela IPC e saem, sem importar o Qt nem os modelos (os atalhos do GNOME usam isso).
"""

import argparse

from . import APP_ID, ipc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_ID, description="Traduz os balões de mangá visíveis na tela.")
    parser.add_argument("--translate", action="store_true", help="traduz a tela agora (na instância em execução, ou abre o app e traduz)")
    parser.add_argument("--colorize", action="store_true", help="mostra a página na tela colorida (na instância em execução, ou abre o app)")
    parser.add_argument("--hide", action="store_true", help="esconde a tradução da instância em execução")
    parser.add_argument("--image", metavar="ARQUIVO", help="traduz uma imagem e grava o resultado, sem abrir o app")
    parser.add_argument("--out", metavar="ARQUIVO", help="com --image: onde gravar (padrão: <nome>-traduzido.png)")
    parser.add_argument("--engine", help="com --image: motor de tradução (padrão: o das configurações)")
    parser.add_argument("--source", help="com --image: idioma de origem (ja, ko, zh-CN, zh-TW, en, auto)")
    parser.add_argument("--color", action="store_true", help="com --image: colore a página também")
    parser.add_argument("--no-translate", action="store_true", help="com --image: não traduz (use com --color)")
    args = parser.parse_args(argv)

    if args.image:
        from .imagefile import translate_file

        return translate_file(
            args.image, args.out, engine=args.engine, source=args.source, colorize=args.color, translate=not args.no_translate
        )

    if args.hide:
        ipc.send("hide")
        return 0
    command = "colorize" if args.colorize else "translate" if args.translate else "show"
    if ipc.send(command):
        return 0

    from .app import run

    return run(start_action=None if command == "show" else command)


if __name__ == "__main__":
    raise SystemExit(main())
