"""Автоопределение категории заявки по ключевым словам.

Неоднозначные случаи (ничего не нашлось или ничья) помечаются ambiguous —
позже их можно будет отдавать в LLM. Диспетчер всегда может исправить категорию.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import Category


@dataclass(frozen=True)
class CategoryGuess:
    code: str
    score: int
    ambiguous: bool


def _normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


class Categorizer:
    def __init__(self, categories: list[Category]) -> None:
        self._fallback = next(c.code for c in categories if c.fallback)
        # Ключевое слово должно совпадать с началом слова: «сеть» не найдётся внутри «посетитель».
        self._patterns = {
            c.code: [re.compile(r"(?<!\w)" + re.escape(_normalize(kw))) for kw in c.keywords]
            for c in categories
            if not c.fallback
        }

    def guess(self, text: str) -> CategoryGuess:
        text = _normalize(text)
        scores = {
            code: sum(1 for p in patterns if p.search(text))
            for code, patterns in self._patterns.items()
        }
        best = max(scores.values(), default=0)
        if best == 0:
            return CategoryGuess(self._fallback, 0, ambiguous=True)
        leaders = [code for code, score in scores.items() if score == best]
        if len(leaders) > 1:
            return CategoryGuess(self._fallback, best, ambiguous=True)
        return CategoryGuess(leaders[0], best, ambiguous=False)
