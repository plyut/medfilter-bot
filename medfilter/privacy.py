"""Скрытие персональных данных до отправки текста в нейросеть (152-ФЗ)."""
import re

_RULES = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[e-mail скрыт]"),
    (re.compile(r"\b\d{3}-\d{3}-\d{3}[ -]?\d{2}\b"), "[СНИЛС скрыт]"),
    (re.compile(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b"), "[телефон скрыт]"),
    (re.compile(r"\b\d{16}\b"), "[номер полиса скрыт]"),
    (re.compile(r"\b\d{4}\s?\d{6}\b"), "[паспорт скрыт]"),
]


def mask_pii(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
