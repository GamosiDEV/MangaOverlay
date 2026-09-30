"""Detectar o idioma de origem pelas falas da página, sem LLM (grátis e offline).

O OCR depende do idioma, então as falas são lidas por dois leitores que cobrem todas as escritas: o de mangá (kana e
ideogramas) e o EasyOCR de coreano (hangul e letras latinas). Cada fala vota pela escrita que aparece:

- hiragana ou katakana: japonês;
- hangul: coreano;
- só ideogramas: chinês. Simplificado ou tradicional: nas falas em chinês, qual dos dois leitores do EasyOCR
  (simplificado e tradicional) lê com mais confiança;
- letras latinas: inglês (o único idioma de escrita latina entre as origens).

Usado pelo "Detectar idioma pela tela", pela conferência antes de importar capítulos e pelo aviso de origem
suspeita durante a leitura.
"""

import re
from collections import Counter
from dataclasses import dataclass

_KANA = re.compile(r"[ぁ-ゖァ-ヺー]")
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿㄱ-ㆎ]")
_HAN = re.compile(r"[一-鿿㐀-䶿]")
_LATIN = re.compile(r"[A-Za-z]")

MIN_VOTES = 2  # falas lidas com escrita clara, no mínimo, para sugerir um idioma
MIN_SHARE = 0.7  # fração dos votos que o idioma vencedor precisa ter


def script_of(text: str) -> str | None:
    """"ja", "ko", "zh", "en" ou None (sem escrita clara) para uma fala."""
    if len(_KANA.findall(text)) >= 1 and len(_KANA.findall(text)) + len(_HAN.findall(text)) >= 2:
        return "ja"
    if len(_HANGUL.findall(text)) >= 2:
        return "ko"
    if len(_HAN.findall(text)) >= 2:
        return "zh"
    if len(_LATIN.findall(text)) >= 3:
        return "en"
    return None


def looks_misread(source: str, detected: int, texts: list[str]) -> bool:
    """Sinais de que o idioma de origem está errado: o leitor leu poucas das falas detectadas, ou, em japonês, as falas
    lidas quase não têm kana (chinês lido como japonês: o leitor de mangá lê os ideogramas, mas não há kana)."""
    if detected >= 3 and len(texts) <= detected // 3:
        return True
    if source == "ja" and len(texts) >= 2:
        without_kana = sum(1 for t in texts if script_of(t) == "zh")
        return without_kana / len(texts) >= 0.7
    return False


def chinese_variant(confidences: list[tuple[float, float]]) -> str | None:
    """"zh-CN", "zh-TW" ou None, pelas confianças (simplificado, tradicional) de cada fala em chinês. Só contam as
    falas que os dois leitores leram: a que um deles não lê (fonte estilizada) não diz nada sobre a forma."""
    simplified = traditional = 0
    for sim, tra in confidences:
        if sim >= 0.3 and tra >= 0.3 and sim != tra:
            simplified += sim > tra
            traditional += tra > sim
    if simplified == traditional:
        return None
    return "zh-CN" if simplified > traditional else "zh-TW"


@dataclass
class Guess:
    language: str | None  # código de SOURCES, ou None se não deu para decidir
    votes: int  # falas que votaram no idioma sugerido
    total: int  # falas com escrita clara
    ambiguous_chinese: bool = False  # chinês, mas sem dizer se é simplificado ou tradicional (sugerido: simplificado)


def decide(scripts: list[str | None], chinese: list[tuple[float, float]]) -> Guess:
    """Junta os votos das falas. `chinese`: confianças (simplificado, tradicional) das falas em chinês."""
    counted = Counter(s for s in scripts if s)
    total = sum(counted.values())
    if total < MIN_VOTES:
        return Guess(None, 0, total)
    script, votes = counted.most_common(1)[0]
    if votes < MIN_VOTES or votes / total < MIN_SHARE:
        return Guess(None, votes, total)
    if script != "zh":
        return Guess(script, votes, total)
    variant = chinese_variant(chinese)
    return Guess(variant or "zh-CN", votes, total, ambiguous_chinese=variant is None)


def read_scripts(ocr, crops: list) -> tuple[list[str | None], list[tuple[float, float]]]:
    """Lê as falas com o leitor de mangá (kana e ideogramas) e o EasyOCR de coreano (hangul e latino) e classifica
    cada uma. Retorna (escrita de cada fala, confianças dos leitores de chinês simplificado e tradicional nas falas
    em chinês)."""
    if not crops:
        return [], []
    manga = ocr.read(crops, "ja")
    korean = ocr.read(crops, "ko")
    scripts: list[str | None] = []
    for (ja_text, ja_conf), (ko_text, ko_conf) in zip(manga, korean):
        ko_script = script_of(ko_text) if ko_conf >= 0.4 else None
        ja_script = script_of(ja_text) if ja_conf >= 0.3 else None
        if ko_script == "ko":
            scripts.append("ko")  # o leitor de mangá "inventa" kana a partir de hangul: o coreano vem primeiro
        elif ja_script in ("ja", "zh"):
            scripts.append(ja_script)
        elif ko_script == "en":
            scripts.append("en")
        else:
            scripts.append(None)
    chinese = [crop for crop, script in zip(crops, scripts) if script == "zh"]
    if not chinese:
        return scripts, []
    # O leitor de mangá escreve os ideogramas na forma japonesa: a forma do chinês vem dos leitores de chinês
    simplified, traditional = ocr.read(chinese, "zh-CN"), ocr.read(chinese, "zh-TW")
    return scripts, [(sim, tra) for (_s, sim), (_t, tra) in zip(simplified, traditional)]
