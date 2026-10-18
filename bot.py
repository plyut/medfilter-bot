"""МедФильтр — Telegram-бот для проверки медицинской информации.

Запуск: python bot.py (настройки в .env, см. README.md)
"""
import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from medfilter import config, media, render, speech
from medfilter.checker import Checker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("medfilter")

cfg = config.load()
checker = Checker(cfg)
dp = Dispatcher()

MAX_IMAGES = 6
ALBUM_WAIT = 1.5  # сек: Telegram присылает фото из альбома отдельными сообщениями
albums: dict[str, list[Message]] = {}

START = (
    "👋 Привет! Я <b>МедФильтр</b>.\n\n"
    "Перешли мне пост, отправь скриншот, фото или короткое видео с советами о здоровье. "
    "Я отсею рекламу и «воду», найду медицинские утверждения и скажу, что из этого правда.\n\n"
    "Для каждого утверждения получишь вердикт, объяснение и ссылки на ВОЗ, Минздрав и научные базы.\n\n"
    "<i>Я не ставлю диагнозов и не заменяю врача.</i>"
)
HELP = (
    "<b>Что можно прислать</b>\n"
    "• текст или пересланное сообщение\n"
    "• ссылку вместе с текстом публикации\n"
    "• фото, скриншот, несколько фото альбомом\n"
    "• видео или «кружок» до 20 МБ: возьму кадры и распознаю речь\n\n"
    "<b>Вердикты</b>\n"
    "✅ подтверждено · ⚠️ частично верно · ❌ опровергнуто · ❔ недостаточно данных\n\n"
    "УДД — уровень достоверности доказательств от 1 (самые надёжные исследования) до 5 (мнение экспертов).\n\n"
    "/privacy — какие данные я храню"
)
PRIVACY = (
    "🔒 <b>Данные</b>\n"
    "• Я не сохраняю твои имя, контакты и переписку.\n"
    "• Телефоны, e-mail, СНИЛС, номера паспортов и полисов скрываются до отправки в нейросеть.\n"
    "• Файлы обрабатываются во временной папке и сразу удаляются.\n"
    "• Сохраняется только результат проверки под анонимным ключом, чтобы одинаковый фейк не проверять дважды. "
    f"Через {cfg.cache_days} дней результат проверяется заново.\n"
    "• Нейросеть: GigaChat (серверы в России), распознавание речи: Yandex SpeechKit."
)
ERROR = "Не получилось проверить материал. Попробуй ещё раз через минуту или отправь его текстом."


def keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Подробнее", callback_data=f"d:{key}"),
         InlineKeyboardButton(text="🔗 Источники", callback_data=f"s:{key}")],
        [InlineKeyboardButton(text="🧠 Как проверить самому", callback_data=f"c:{key}"),
         InlineKeyboardButton(text="🔄 Новая проверка", callback_data="new")],
    ])


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(START)


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(HELP)


@dp.message(Command("privacy"))
async def cmd_privacy(msg: Message):
    await msg.answer(PRIVACY)


def origin_note(msg: Message) -> str:
    """Откуда переслано: помогает нейросети оценить источник."""
    o = msg.forward_origin
    if not o:
        return ""
    chat = getattr(o, "chat", None) or getattr(o, "sender_chat", None)
    if chat and chat.title:
        return f"[Переслано из канала/чата «{chat.title}»]\n"
    return "[Пересланное сообщение]\n"


async def collect(msgs: list[Message], bot: Bot) -> tuple[str, str, list[tuple[str, bytes]]]:
    """Собирает текст, изображения и расшифровку речи из одного сообщения или альбома."""
    texts, images, transcript = [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        for m in msgs:
            body = m.text or m.caption or ""
            if body:
                texts.append(origin_note(m) + body)
            if m.photo and len(images) < MAX_IMAGES:
                data = (await bot.download(m.photo[-1])).read()
                images.append((f"фото {len(images) + 1}", data))
            elif m.document and (m.document.mime_type or "").startswith("image/") and len(images) < MAX_IMAGES:
                data = (await bot.download(m.document)).read()
                images.append((m.document.file_name or "изображение", await media.to_jpeg(data)))
            video = m.video or m.video_note or m.animation or (
                m.document if (m.document and (m.document.mime_type or "").startswith("video/")) else None)
            if video:
                path = Path(tmp) / f"v{m.message_id}.mp4"
                await bot.download(video, destination=path)
                room = MAX_IMAGES - len(images)
                if room > 0:
                    frames = await media.video_frames(path, count=min(4, room))
                    images += [(f"кадр видео {i + 1}", f) for i, f in enumerate(frames)]
                try:
                    transcript.append(await speech.recognize(cfg, await media.audio_chunks(path)))
                except RuntimeError:
                    log.info("В видео нет звука или его не удалось извлечь")
    return "\n\n".join(texts), " ".join(t for t in transcript if t), images


async def run_check(msgs: list[Message], bot: Bot):
    first = msgs[0]
    status = await first.reply("⏳ Проверяю. Обычно это занимает 20–60 секунд…")
    try:
        text, transcript, images = await collect(msgs, bot)
        if not (text or transcript or images):
            await status.edit_text("Пришли текст, фото или видео, и я проверю, что в нём правда.")
            return
        key, result, cached = await checker.check(text, transcript, images)
        await status.edit_text(render.short(result, cached), reply_markup=keyboard(key), disable_web_page_preview=True)
    except TelegramBadRequest as e:
        log.warning("Telegram: %s", e)
        await status.edit_text("Файл слишком большой: Telegram отдаёт ботам файлы до 20 МБ. Пришли видео покороче или скриншоты.")
    except Exception:
        log.exception("Ошибка проверки")
        await status.edit_text(ERROR)


@dp.message(F.media_group_id)
async def on_album(msg: Message, bot: Bot):
    gid = msg.media_group_id
    if gid in albums:
        albums[gid].append(msg)
        return
    albums[gid] = [msg]
    await asyncio.sleep(ALBUM_WAIT)
    await run_check(albums.pop(gid), bot)


@dp.message(F.text | F.caption | F.photo | F.video | F.video_note | F.animation | F.document)
async def on_message(msg: Message, bot: Bot):
    await run_check([msg], bot)


@dp.message()
async def on_other(msg: Message):
    await msg.answer("Я понимаю текст, фото и видео. Голосовые и стикеры пока не проверяю.")


@dp.callback_query(F.data == "new")
async def on_new(cb: CallbackQuery):
    await cb.message.answer("Присылай следующий материал: текст, фото или видео.")
    await cb.answer()


@dp.callback_query(F.data.regexp(r"^[dsc]:"))
async def on_view(cb: CallbackQuery):
    view, key = cb.data.split(":", 1)
    result = checker.get(key)
    if not result:
        await cb.answer("Результат устарел, пришли материал ещё раз.", show_alert=True)
        return
    text = {"d": render.details, "s": render.sources, "c": render.self_check}[view](result)
    await cb.message.answer(text, reply_markup=keyboard(key), disable_web_page_preview=True)
    await cb.answer()


async def main():
    bot = Bot(cfg.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    log.info("МедФильтр запущен. Распознавание речи: %s", "включено" if cfg.speech_enabled else "выключено")
    try:
        await dp.start_polling(bot)
    finally:
        await checker.llm.close()


if __name__ == "__main__":
    asyncio.run(main())
