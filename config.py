"""Настройки бота из переменных окружения (.env)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_token: str
    gigachat_credentials: str
    gigachat_scope: str
    gigachat_model: str
    gigachat_verify_ssl: bool
    yandex_api_key: str
    yandex_folder_id: str
    max_parallel_checks: int
    cache_days: int
    data_dir: Path
    # Этап 2: апелляции
    retention_days: int = 180    # сколько хранятся доводы апелляций и подписки на обновления
    appeals_per_day: int = 5     # лимит апелляций от одного пользователя в сутки

    @property
    def speech_enabled(self) -> bool:
        return bool(self.yandex_api_key and self.yandex_folder_id)


def load() -> Config:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    creds = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
    if not token or not creds:
        raise SystemExit("Заполните TELEGRAM_TOKEN и GIGACHAT_CREDENTIALS в файле .env (см. .env.example)")
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        telegram_token=token,
        gigachat_credentials=creds,
        gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        gigachat_model=os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max"),
        gigachat_verify_ssl=os.getenv("GIGACHAT_VERIFY_SSL", "false").lower() == "true",
        yandex_api_key=os.getenv("YANDEX_API_KEY", "").strip(),
        yandex_folder_id=os.getenv("YANDEX_FOLDER_ID", "").strip(),
        max_parallel_checks=int(os.getenv("MAX_PARALLEL_CHECKS", "1")),
        cache_days=int(os.getenv("CACHE_DAYS", "30")),
        data_dir=data_dir,
        retention_days=int(os.getenv("RETENTION_DAYS", "180")),
        appeals_per_day=int(os.getenv("APPEALS_PER_DAY", "5")),
    )
