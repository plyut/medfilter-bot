"""Кэш результатов проверки.

Ключ — хэш содержимого материала, поэтому одинаковый фейк, который пересылают тысячи
людей, проверяется один раз. Хранятся только хэш, результат и время, без данных о
пользователе. Через CACHE_DAYS дней запись устаревает и материал проверяется заново.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path


def make_key(text: str, transcript: str, images: list[bytes]) -> str:
    h = hashlib.sha256()
    h.update(" ".join(text.lower().split()).encode())
    h.update(b"\0" + " ".join(transcript.lower().split()).encode())
    for img in images:
        h.update(b"\0" + hashlib.sha256(img).digest())
    return h.hexdigest()[:24]


class Cache:
    def __init__(self, path: Path, days: int):
        self.ttl = days * 86400
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, result TEXT, created REAL)")
        self.db.commit()

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT result, created FROM results WHERE key = ?", (key,)).fetchone()
        if not row or time.time() - row[1] > self.ttl:
            return None
        return json.loads(row[0])

    def put(self, key: str, result: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?)",
                        (key, json.dumps(result, ensure_ascii=False), time.time()))
        self.db.commit()

    def stats(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
