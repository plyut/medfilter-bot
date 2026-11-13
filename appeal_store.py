"""Хранилище апелляций, версий вердикта и подписок на обновления (SQLite).

Что хранится и зачем:
- appeals — обращения. Вместо Telegram ID — анонимный хэш пользователя. Доводы хранятся
  RETENTION_DAYS дней (или до /delete), потом стираются; остаётся только решение и отпечаток
  доводов, чтобы повторная жалоба с теми же аргументами получила ссылку на прежнее решение.
- versions — история вердикта по каждому утверждению: дата, вердикт, основание изменения.
  Персональных данных нет.
- subscribers — кто проверял материал, чтобы прислать уведомление, если вердикт изменится.
  Хранится chat_id, поэтому запись удаляется через RETENTION_DAYS дней или по /delete.
"""
import json
import sqlite3
import time
from pathlib import Path

DAY = 86400


class AppealStore:
    def __init__(self, path: Path, retention_days: int = 180):
        self.retention = retention_days * DAY
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS appeals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_key TEXT NOT NULL, claim_idx INTEGER NOT NULL,
                user_hash TEXT, reason TEXT, argument TEXT, url TEXT,
                fingerprint TEXT, tokens TEXT,
                status TEXT NOT NULL, outcome TEXT, decision TEXT,
                created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS appeals_claim ON appeals(material_key, claim_idx);
            CREATE INDEX IF NOT EXISTS appeals_user ON appeals(user_hash);
            CREATE TABLE IF NOT EXISTS versions (
                material_key TEXT NOT NULL, claim_idx INTEGER NOT NULL, version INTEGER NOT NULL,
                verdict TEXT, confidence INTEGER, udd INTEGER, explanation TEXT,
                disputed INTEGER DEFAULT 0, reason TEXT, appeal_id INTEGER, created REAL NOT NULL,
                PRIMARY KEY (material_key, claim_idx, version));
            CREATE TABLE IF NOT EXISTS subscribers (
                material_key TEXT NOT NULL, chat_id INTEGER NOT NULL, created REAL NOT NULL,
                PRIMARY KEY (material_key, chat_id));
        """)
        self.db.commit()

    # ---------- апелляции ----------
    def create(self, material_key: str, claim_idx: int, user_hash: str, reason: str, argument: str,
               url: str, fingerprint: str, tokens: str, status: str) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO appeals (material_key, claim_idx, user_hash, reason, argument, url, fingerprint, tokens,"
            " status, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (material_key, claim_idx, user_hash, reason, argument, url, fingerprint, tokens, status, now, now))
        self.db.commit()
        return cur.lastrowid

    def set_status(self, appeal_id: int, status: str) -> None:
        self.db.execute("UPDATE appeals SET status = ?, updated = ? WHERE id = ?", (status, time.time(), appeal_id))
        self.db.commit()

    def finish(self, appeal_id: int, status: str, outcome: str | None, decision: dict) -> None:
        self.db.execute("UPDATE appeals SET status = ?, outcome = ?, decision = ?, updated = ? WHERE id = ?",
                        (status, outcome, json.dumps(decision, ensure_ascii=False), time.time(), appeal_id))
        self.db.commit()

    def get(self, appeal_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
        return self._row(row)

    def by_user(self, user_hash: str, limit: int = 10) -> list[dict]:
        rows = self.db.execute("SELECT * FROM appeals WHERE user_hash = ? ORDER BY id DESC LIMIT ?",
                               (user_hash, limit)).fetchall()
        return [self._row(r) for r in rows]

    def count_since(self, user_hash: str, seconds: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM appeals WHERE user_hash = ? AND created > ?",
                               (user_hash, time.time() - seconds)).fetchone()[0]

    def for_claim(self, material_key: str, claim_idx: int) -> list[dict]:
        rows = self.db.execute("SELECT * FROM appeals WHERE material_key = ? AND claim_idx = ? ORDER BY id",
                               (material_key, claim_idx)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> dict | None:
        if not row:
            return None
        d = dict(row)
        d["decision"] = json.loads(d["decision"]) if d.get("decision") else None
        return d

    # ---------- версии вердикта ----------
    def versions(self, material_key: str, claim_idx: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM versions WHERE material_key = ?", [material_key]
        if claim_idx is not None:
            q += " AND claim_idx = ?"
            args.append(claim_idx)
        rows = self.db.execute(q + " ORDER BY claim_idx, version", args).fetchall()
        return [dict(r) for r in rows]

    def add_version(self, material_key: str, claim_idx: int, claim: dict, reason: str,
                    appeal_id: int | None = None, disputed: bool = False) -> int:
        last = self.db.execute("SELECT MAX(version) FROM versions WHERE material_key = ? AND claim_idx = ?",
                               (material_key, claim_idx)).fetchone()[0] or 0
        self.db.execute(
            "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (material_key, claim_idx, last + 1, str(claim.get("verdict") or "none"), _int(claim.get("confidence")),
             _int(claim.get("udd")), str(claim.get("explanation") or ""), int(disputed), reason, appeal_id,
             time.time()))
        self.db.commit()
        return last + 1

    def ensure_first_version(self, material_key: str, claim_idx: int, claim: dict, created: float | None = None):
        """Первая версия — исходный вердикт проверки. Записывается при первой апелляции."""
        if not self.versions(material_key, claim_idx):
            self.add_version(material_key, claim_idx, claim, "первичная проверка")
            if created:
                self.db.execute("UPDATE versions SET created = ? WHERE material_key = ? AND claim_idx = ?",
                                (created, material_key, claim_idx))
                self.db.commit()

    # ---------- подписки на уведомления ----------
    def subscribe(self, material_key: str, chat_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO subscribers VALUES (?,?,?)", (material_key, chat_id, time.time()))
        self.db.commit()

    def subscribers(self, material_key: str) -> list[int]:
        rows = self.db.execute("SELECT chat_id FROM subscribers WHERE material_key = ? AND created > ?",
                               (material_key, time.time() - self.retention)).fetchall()
        return [r[0] for r in rows]

    # ---------- приватность ----------
    def scrub(self, appeal_id: int) -> None:
        """Стирает доводы одного обращения (например, если прислали медицинский документ)."""
        self.db.execute("UPDATE appeals SET argument = NULL, url = NULL, tokens = NULL, fingerprint = NULL "
                        "WHERE id = ?", (appeal_id,))
        self.db.commit()

    def forget_user(self, user_hash: str, chat_id: int) -> tuple[int, int]:
        """/delete: стирает доводы и связь с пользователем, удаляет подписки. Решения остаются обезличенными."""
        a = self.db.execute("UPDATE appeals SET user_hash = NULL, argument = NULL, url = NULL, tokens = NULL "
                            "WHERE user_hash = ?", (user_hash,)).rowcount
        s = self.db.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,)).rowcount
        self.db.commit()
        return a, s

    def purge(self) -> None:
        """Срок хранения: старые доводы обезличиваются, старые подписки удаляются."""
        border = time.time() - self.retention
        self.db.execute("UPDATE appeals SET user_hash = NULL, argument = NULL, url = NULL, tokens = NULL "
                        "WHERE created < ?", (border,))
        self.db.execute("DELETE FROM subscribers WHERE created < ?", (border,))
        self.db.commit()

    def close(self):
        self.db.close()


def _int(x):
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return None
