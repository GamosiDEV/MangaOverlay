"""Idiomas de origem (mangá, manhwa, manhua, scans em inglês) e de destino."""

import re

AUTO = "auto"

# Origem: código -> (nome em português, nome em inglês, código NLLB, padrão de escrita esperado no OCR)
SOURCES: dict[str, tuple[str, str, str, str]] = {
    "ja": ("Japonês (mangá)", "Japanese", "jpn_Jpan", r"[぀-ヿ一-鿿]"),
    "ko": ("Coreano (manhwa)", "Korean", "kor_Hang", r"[가-힯ᄀ-ᇿ]"),
    "zh-CN": ("Chinês simplificado (manhua)", "Simplified Chinese", "zho_Hans", r"[一-鿿]"),
    "zh-TW": ("Chinês tradicional (manhua)", "Traditional Chinese", "zho_Hant", r"[一-鿿]"),
    "en": ("Inglês", "English", "eng_Latn", r"[A-Za-z]{2}"),
}

# Destino: código -> (nome em português, nome em inglês, código NLLB)
TARGETS: dict[str, tuple[str, str, str]] = {
    "pt": ("Português", "Brazilian Portuguese", "por_Latn"),
    "en": ("Inglês", "English", "eng_Latn"),
    "es": ("Espanhol", "Spanish", "spa_Latn"),
    "fr": ("Francês", "French", "fra_Latn"),
    "de": ("Alemão", "German", "deu_Latn"),
    "it": ("Italiano", "Italian", "ita_Latn"),
}


def source_name(code: str) -> str:
    if code == AUTO:
        return "Detectar (só com LLM lendo a imagem)"
    return SOURCES[code][0] if code in SOURCES else code


def target_name(code: str) -> str:
    return TARGETS[code][0] if code in TARGETS else code


def source_english(code: str) -> str:
    return SOURCES[code][1] if code in SOURCES else "an unknown language"


def target_english(code: str) -> str:
    return TARGETS[code][1] if code in TARGETS else code


def looks_like(text: str, source: str) -> bool:
    """O texto lido pelo OCR tem a escrita do idioma de origem? Filtra lixo e textos da interface do sistema."""
    if source not in SOURCES:
        return bool(text.strip())
    return re.search(SOURCES[source][3], text) is not None
