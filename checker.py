"""Конвейер проверки: кэш → очередь → нейросеть → разбор ответа."""
import asyncio
import logging

from .cache import Cache, make_key
from .config import Config
from .llm import LLM
from .privacy import mask_pii
from .prompt import build_user_prompt, parse_json

log = logging.getLogger(__name__)


class Checker:
    def __init__(self, cfg: Config, llm: LLM | None = None):
        self.cfg = cfg
        self.llm = llm or LLM(cfg)
        self.cache = Cache(cfg.data_dir / "cache.sqlite3", cfg.cache_days)
        # Очередь: одновременно к нейросети уходит не больше N запросов, остальные ждут.
        self.slots = asyncio.Semaphore(cfg.max_parallel_checks)

    async def check(self, text: str, transcript: str, images: list[tuple[str, bytes]]) -> tuple[str, dict, bool]:
        """Возвращает (ключ результата, результат, взят ли из кэша)."""
        text, transcript = mask_pii(text.strip()), mask_pii(transcript.strip())
        key = make_key(text, transcript, [data for _, data in images])
        cached = self.cache.get(key)
        if cached:
            return key, cached, True

        async with self.slots:
            described, seen = [], set()
            for name, data in images:
                try:
                    desc = mask_pii(await self.llm.describe(name, data))
                except Exception:
                    log.exception("Не удалось разобрать изображение %s", name)
                    continue
                # Кадры статичного видео дают одинаковые описания: оставляем одно.
                norm = " ".join(desc.lower().split())
                if norm not in seen:
                    seen.add(norm)
                    described.append((name, desc))
            prompt = build_user_prompt(text, transcript, described)
            reply = await self.llm.ask(prompt)
            try:
                result = parse_json(reply)
            except ValueError:
                log.info("Ответ не JSON, повторяю запрос один раз")
                reply = await self.llm.ask(prompt + "\n\nВерни ответ строго в формате JSON.")
                result = parse_json(reply)
        self.cache.put(key, result)
        return key, result, False

    def get(self, key: str) -> dict | None:
        return self.cache.get(key)
