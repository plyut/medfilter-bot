"""Обращение к GigaChat: текст + изображения."""
import logging

from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

from .config import Config
from .prompt import SYSTEM

log = logging.getLogger(__name__)


class LLM:
    def __init__(self, cfg: Config):
        self.client = GigaChat(
            credentials=cfg.gigachat_credentials,
            scope=cfg.gigachat_scope,
            model=cfg.gigachat_model,
            verify_ssl_certs=cfg.gigachat_verify_ssl,
            timeout=180,
        )

    async def describe(self, name: str, data: bytes) -> str:
        """Шаг извлечения: дословный текст с изображения и краткое описание того, что на нём."""
        uploaded = await self.client.aupload_file((name, data, "image/jpeg"))
        resp = await self.client.achat(Chat(messages=[Messages(
            role=MessagesRole.USER, attachments=[uploaded.id_],
            content="Перепиши дословно весь текст на изображении. Затем одним-двумя предложениями опиши, "
                    "что изображено (люди, продукты, упаковки, графики). Ничего не оценивай.")],
            temperature=0.1, max_tokens=800))
        return resp.choices[0].message.content.strip()

    async def ask(self, prompt: str) -> str:
        messages = [Messages(role=MessagesRole.SYSTEM, content=SYSTEM), Messages(role=MessagesRole.USER, content=prompt)]
        resp = await self.client.achat(Chat(messages=messages, temperature=0.2, max_tokens=3000))
        return resp.choices[0].message.content

    async def close(self):
        await self.client.aclose()
