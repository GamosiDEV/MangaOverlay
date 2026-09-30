"""Detectar o idioma de origem pela escrita das falas (python -m pytest tests)."""

from mangaoverlay.langdetect import chinese_variant, decide, script_of
from mangaoverlay.pipeline import _looks_misread


def test_escrita_de_cada_fala():
    assert script_of("お前は誰だ?") == "ja"
    assert script_of("早く逃げて!") == "ja"  # ideogramas com kana: japonês
    assert script_of("여기서 뭐 하는 거야?") == "ko"
    assert script_of("有烦恼随时告诉我!") == "zh"
    assert script_of("這個問題很難說") == "zh"
    assert script_of("What are you doing?") == "en"
    assert script_of("!!") is None
    assert script_of("?") is None


def test_votacao():
    guess = decide(["zh", "zh", "zh", None, "ja"], [(0.9, 0.6), (0.95, 0.7)])
    assert (guess.language, guess.votes, guess.total) == ("zh-CN", 3, 4)
    assert decide(["ja", "ja", "ja", "ja"], []).language == "ja"
    assert decide(["ko"], []).language is None  # uma fala só não basta
    assert decide(["ja", "zh", "ko", "en"], []).language is None  # sem maioria clara


def test_chines_simplificado_ou_tradicional():
    assert chinese_variant([(0.96, 0.62), (0.89, 0.58), (0.96, 0.98)]) == "zh-CN"
    assert chinese_variant([(0.71, 0.77), (0.0, 1.0), (0.95, 0.98)]) == "zh-TW"
    # A fala que um dos leitores não lê (fonte estilizada) não conta
    assert chinese_variant([(0.93, 0.77), (0.0, 0.63)]) == "zh-CN"
    assert chinese_variant([(0.0, 0.9)]) is None
    ambiguous = decide(["zh", "zh"], [(0.0, 0.9), (0.9, 0.0)])
    assert ambiguous.language == "zh-CN" and ambiguous.ambiguous_chinese


def test_sinais_de_origem_errada():
    # Chinês lido como japonês: ideogramas sem kana
    assert _looks_misread("ja", 2, ["有烦恼随时告诉我", "这个老师会帮你"])
    assert not _looks_misread("ja", 4, ["お前は誰だ", "ここはどこなの", "早く逃げて", "ありがとう"])
    # O leitor do idioma leu poucas das falas detectadas
    assert _looks_misread("ko", 6, ["여기서"])
    assert not _looks_misread("ko", 6, ["여기서", "빨리 가자", "고마워", "어려워"])
