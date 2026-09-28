"""Configurações do usuário, salvas em JSON na pasta de config do sistema."""

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir

from . import APP_NAME
from .languages import SOURCES, TARGETS

CONFIG_DIR = Path(user_config_dir(APP_NAME, appauthor=False))
CACHE_DIR = Path(user_cache_dir(APP_NAME, appauthor=False))
CONFIG_FILE = CONFIG_DIR / "config.json"

# Motor de leitura/tradução: chave -> rótulo exibido
ENGINES = {
    "local": "OCR local + tradução offline (NLLB, na GPU)",
    "google": "OCR local + Google Tradutor gratuito",
    "openai-text": "OCR local + OpenAI GPT (texto)",
    "openai-vision": "OpenAI GPT lê a imagem e traduz",
    "claude-text": "OCR local + Claude (texto)",
    "claude-vision": "Claude lê a imagem e traduz",
}
VISION_ENGINES = {"openai-vision", "claude-vision"}
OPENAI_MODELS = ["gpt-4.1-mini", "gpt-4.1", "gpt-5-mini", "gpt-5"]
CLAUDE_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]


@dataclass
class Config:
    engine: str = "local"
    openai_model: str = "gpt-4.1-mini"
    claude_model: str = "claude-opus-5"
    source_lang: str = "ja"
    # Obra que está sendo lida (id no banco); None = sem obra
    current_work: int | None = None
    target_lang: str = "pt"
    # Traduz também textos fora dos balões (narração, onomatopeias). Desligado por padrão porque
    # o detector confunde esses textos com os de menus e sites.
    include_free_text: bool = False
    font_family: str = ""
    # Mostra a página colorida junto com a tradução (o atalho de colorir funciona sempre)
    colorize: bool = False
    hotkey_translate: str = "Ctrl+Alt+M"
    hotkey_colorize: str = "Ctrl+Alt+C"
    hotkey_hide: str = "Ctrl+Alt+N"
    # Usa a GPU (CUDA) para os modelos locais, se houver
    use_gpu: bool = True

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        config = cls(**{k: v for k, v in data.items() if k in known})
        if config.engine not in ENGINES:
            config.engine = cls.engine
        if config.source_lang not in SOURCES and config.source_lang != "auto":
            config.source_lang = cls.source_lang
        if config.target_lang not in TARGETS:
            config.target_lang = cls.target_lang
        return config

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_FILE)

    @property
    def hotkeys(self) -> dict[str, str]:
        return {"translate": self.hotkey_translate, "colorize": self.hotkey_colorize, "hide": self.hotkey_hide}
