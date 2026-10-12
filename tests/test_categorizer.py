import pytest

from app.categorizer import Categorizer


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Не работает розетка у окна", "electric"),
        ("Нет интернета в кабинете", "network"),
        ("Не включается компьютер преподавателя", "computers"),
        ("Сломался стул", "furniture"),
        ("Течёт кран в туалете", "plumbing"),
        ("Не работает Wi-Fi", "network"),
        ("ПРОЕКТОР НЕ ВКЛЮЧАЕТСЯ", "computers"),
        ("Холодно, батарея еле тёплая", "plumbing"),
    ],
)
def test_guess(org, text, expected):
    guess = Categorizer(org.categories).guess(text)
    assert guess.code == expected
    assert not guess.ambiguous


def test_unknown_goes_to_fallback(org):
    guess = Categorizer(org.categories).guess("Пахнет чем-то странным")
    assert guess.code == "other"
    assert guess.ambiguous


def test_keyword_matches_only_word_start(org):
    # «сеть» не должна находиться внутри «посетители»
    guess = Categorizer(org.categories).guess("Посетители жалуются")
    assert guess.code == "other"


def test_tie_is_ambiguous(org):
    guess = Categorizer(org.categories).guess("Не работает свет и интернет")
    assert guess.ambiguous
    assert guess.code == "other"
