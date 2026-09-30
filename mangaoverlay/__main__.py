"""Linha de comando.

Mantido leve de propósito: quando o app já está rodando, `--translate`, `--colorize` e `--hide` só enviam
um comando pela IPC e saem, sem importar o Qt nem os modelos (os atalhos do GNOME usam isso).
"""

import argparse
import os
import sys

from . import APP_ID, APP_NAME, ipc

_LOG_LIMIT = 5 * 1024 * 1024


def _prepare_streams() -> None:
    """Aberto pelo atalho do Windows (pythonw.exe), o app não tem console: stdout e stderr são None, e qualquer
    print ou barra de progresso de download derrubaria o processo. Nesse caso a saída vai para um arquivo de log."""
    if sys.stdout is None or sys.stderr is None:
        from pathlib import Path

        from platformdirs import user_log_dir

        log_dir = Path(user_log_dir(APP_NAME, appauthor=False))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{APP_ID}.log"
        mode = "w" if log_file.exists() and log_file.stat().st_size > _LOG_LIMIT else "a"
        log = open(log_file, mode, encoding="utf-8", buffering=1)  # noqa: SIM115 (fica aberto até o fim)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    # Console ou pipe do Windows em cp1252: texto japonês não pode derrubar um print
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    # Num crash nativo (Qt, PyTorch…), grava a pilha de todas as threads no stderr/log
    import faulthandler

    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except (AttributeError, OSError, ValueError):  # stderr sem descritor de arquivo
        pass


def main(argv: list[str] | None = None) -> int:
    _prepare_streams()
    # O NLLB e o manga-ocr só têm pytorch_model.bin no repositório. Depois de carregar o .bin, o transformers
    # baixava em segundo plano uma cópia em .safetensors "para a próxima vez": ~2,7 GB a mais, sem uso.
    os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "1")
    if sys.platform == "win32":
        # Sem o modo de desenvolvedor, o Windows não cria symlinks: o cache do Hugging Face copia os arquivos e avisa
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

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
    parser.add_argument("--download-models", action="store_true", help="baixa todos os modelos locais agora (o instalador usa)")
    parser.add_argument("--export", metavar="ARQUIVO", help="exporta obras, capítulos e traduções para um .zip")
    parser.add_argument("--obra", action="append", metavar="NOME", help="com --export: só esta obra (pode repetir); sem ela, todas")
    parser.add_argument("--import", dest="import_file", metavar="ARQUIVO", help="importa um .zip exportado (mescla, sem sobrescrever)")
    args = parser.parse_args(argv)

    if args.export or args.import_file:
        from .transfer import run_cli

        return run_cli(args.export, args.obra, args.import_file)

    if args.download_models:
        from .models import download_all

        return download_all()

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
