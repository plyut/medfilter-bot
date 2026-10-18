"""Распознавание речи из видео: Yandex SpeechKit, синхронный API (куски до 30 с)."""
import logging

import aiohttp

from .config import Config

log = logging.getLogger(__name__)
URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


async def recognize(cfg: Config, chunks: list[bytes]) -> str:
    if not cfg.speech_enabled or not chunks:
        return ""
    params = {"lang": "ru-RU", "folderId": cfg.yandex_folder_id, "format": "oggopus"}
    headers = {"Authorization": f"Api-Key {cfg.yandex_api_key}"}
    parts = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        for chunk in chunks:
            try:
                async with session.post(URL, params=params, headers=headers, data=chunk) as r:
                    body = await r.json(content_type=None)
                    if r.status == 200 and body.get("result"):
                        parts.append(body["result"])
                    elif r.status != 200:
                        log.warning("SpeechKit %s: %s", r.status, body)
            except aiohttp.ClientError as e:
                log.warning("SpeechKit недоступен: %s", e)
    return " ".join(parts)
