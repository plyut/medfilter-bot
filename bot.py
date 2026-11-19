"""МедФильтр — Telegram-бот для проверки медицинской информации.

Запуск: python bot.py (настройки в .env, см. README.md)
"""
import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from medfilter import appeal_render, config, med_docs, media, render, speech
from medfilter.appeal_store import DAY, AppealStore
from medfilter.appeals import REASONS, RESOLVED, AppealService, Evidence
from medfilter.checker import Checker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("medfilter")

cfg = config.load()
checker = Checker(cfg)
store = AppealStore(cfg.data_dir / "appeals.sqlite3", cfg.retention_days)
appeals = AppealService(checker, store, salt=cfg.telegram_token, appeals_per_day=cfg.appeals_per_day)
dp = Dispatcher()

MAX_IMAGES = 6
ALBUM_WAIT = 1.5  # сек: Telegram присылает фото из альбома отдельными сообщениями
albums: dict[str, list[Message]] = {}
appeal_albums: dict[str, list[Message]] = {}


class AppealFlow(StatesGroup):
    evidence = State()  # ждём ссылку, текст, цитату или скриншот


START = (
    "👋 Привет! Я <b>МедФильтр</b>.\n\n"
    "Перешли мне пост, отправь скриншот, фото или короткое видео с советами о здоровье. "
    "Я отсею рекламу и «воду», найду медицинские утверждения и скажу, что из этого правда.\n\n"
    "Для каждого утверждения получишь вердикт, объяснение и ссылки на ВОЗ, Минздрав и научные базы. "
    "Не согласен с вердиктом — нажми «⚖️ Оспорить», и я честно перепроверю.\n\n"
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
    "<b>Апелляции</b>\n"
    "«⚖️ Оспорить» под результатом — выбери утверждение и причину, приложи ссылку, текст или скриншот. "
    "Получишь номер обращения и ответ «было → стало».\n"
    "/appeals — мои апелляции и их статусы\n"
    "/cancel — отменить начатую апелляцию\n\n"
    "/privacy — какие данные я храню\n"
    "/delete — удалить мои данные"
)
PRIVACY = (
    "🔒 <b>Данные</b>\n"
    "• Я не сохраняю твои имя, контакты и переписку.\n"
    "• Телефоны, e-mail, СНИЛС, номера паспортов и полисов скрываются до отправки в нейросеть.\n"
    "• Файлы обрабатываются во временной папке и сразу удаляются.\n"
    "• Сохраняется только результат проверки под анонимным ключом, чтобы одинаковый фейк не проверять дважды. "
    f"Через {cfg.cache_days} дней результат проверяется заново.\n"
    "• Апелляции хранятся под анонимным ID, а не под Telegram ID. Твои доводы хранятся "
    f"{cfg.retention_days} дней, потом стираются; решение остаётся обезличенным.\n"
    "• Чтобы прислать уведомление, если вердикт изменится, я помню, в каком чате проверялся материал, "
    f"тоже {cfg.retention_days} дней.\n"
    "• Личные медицинские документы (выписки, анализы, рецепты) я не принимаю.\n"
    "• /delete удаляет твои доводы и подписки на уведомления.\n"
    "• Нейросеть: GigaChat (серверы в России), распознавание речи: Yandex SpeechKit."
)
ERROR = "Не получилось проверить материал. Попробуй ещё раз через минуту или отправь его текстом."


def keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Подробнее", callback_data=f"d:{key}"),
         InlineKeyboardButton(text="🔗 Источники", callback_data=f"s:{key}")],
        [InlineKeyboardButton(text="🧠 Как проверить самому", callback_data=f"c:{key}"),
         InlineKeyboardButton(text="🕓 История", callback_data=f"h:{key}")],
        [InlineKeyboardButton(text="⚖️ Оспорить", callback_data=f"ap:{key}"),
         InlineKeyboardButton(text="🔄 Новая проверка", callback_data="new")],
    ])


def after_appeal_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Обновлённый разбор", callback_data=f"d:{key}"),
         InlineKeyboardButton(text="🕓 История", callback_data=f"h:{key}")],
    ])


def open_appeal_keyboard(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Открыть прежнее решение", callback_data=f"av:{appeal_id}")]])


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(START)


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(HELP)


@dp.message(Command("privacy"))
async def cmd_privacy(msg: Message):
    await msg.answer(PRIVACY)


# ---------------- апелляции: команды ----------------

@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Апелляция отменена. Можешь прислать новый материал для проверки.")


@dp.message(Command("appeals"))
async def cmd_appeals(msg: Message):
    rows = store.by_user(appeals.user_hash(msg.from_user.id))
    buttons = [[InlineKeyboardButton(text=f"📄 {appeal_render.number(a['id'])}", callback_data=f"av:{a['id']}")]
               for a in rows if a["status"] == RESOLVED][:8]
    await msg.answer(appeal_render.appeals_list(rows),
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)


@dp.message(Command("delete"))
async def cmd_delete(msg: Message):
    await msg.answer(
        "Удалить твои данные? Сотру доводы из твоих апелляций, связь апелляций с тобой и подписки на уведомления. "
        "Обезличенные решения и история вердиктов останутся: в них нет данных о тебе.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data="del:yes"),
            InlineKeyboardButton(text="Отмена", callback_data="del:no")]]))


@dp.callback_query(F.data.startswith("del:"))
async def on_delete(cb: CallbackQuery, state: FSMContext):
    if cb.data == "del:yes":
        await state.clear()
        a, s = store.forget_user(appeals.user_hash(cb.from_user.id), cb.message.chat.id)
        await cb.message.edit_text(f"🗑 Готово. Обезличено апелляций: {a}, удалено подписок: {s}.")
    else:
        await cb.message.edit_text("Ничего не удалено.")
    await cb.answer()


# ---------------- апелляции: шаги в чате ----------------

@dp.callback_query(F.data.startswith("ap:"))
async def on_appeal_start(cb: CallbackQuery, state: FSMContext):
    key = cb.data[3:]
    result = checker.get(key)
    claims = (result or {}).get("claims") or []
    if not claims:
        await cb.answer("Здесь нет утверждений, которые можно оспорить, или результат устарел.", show_alert=True)
        return
    await state.clear()
    if len(claims) == 1:
        await ask_reason(cb.message, key, 0, claims[0])
    else:
        rows = [[InlineKeyboardButton(text=f"{i}. {str(c.get('claim', ''))[:50]}", callback_data=f"ac:{key}:{i - 1}")]
                for i, c in enumerate(claims, 1)]
        await cb.message.answer(appeal_render.choose_claim(result), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


async def ask_reason(target: Message, key: str, idx: int, claim: dict):
    rows = [[InlineKeyboardButton(text=label, callback_data=f"ar:{key}:{idx}:{code}")] for code, label in REASONS.items()]
    await target.answer(appeal_render.choose_reason(claim), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("ac:"))
async def on_appeal_claim(cb: CallbackQuery):
    _, key, idx = cb.data.split(":")
    claims = (checker.get(key) or {}).get("claims") or []
    i = int(idx)
    if not (0 <= i < len(claims)):
        await cb.answer("Результат устарел, пришли материал ещё раз.", show_alert=True)
        return
    await ask_reason(cb.message, key, i, claims[i])
    await cb.answer()


@dp.callback_query(F.data.startswith("ar:"))
async def on_appeal_reason(cb: CallbackQuery, state: FSMContext):
    _, key, idx, code = cb.data.split(":")
    await state.set_state(AppealFlow.evidence)
    await state.update_data(key=key, idx=int(idx), reason=code)
    await cb.message.answer(appeal_render.ask_evidence(REASONS.get(code, REASONS["oth"])),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="Оспорить без источника", callback_data="an"),
                                InlineKeyboardButton(text="Отмена", callback_data="ax")]]))
    await cb.answer()


@dp.callback_query(F.data == "ax")
async def on_appeal_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Апелляция отменена.")
    await cb.answer()


@dp.callback_query(F.data == "an")
async def on_appeal_no_source(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data:
        await cb.answer("Начни апелляцию заново кнопкой «⚖️ Оспорить».", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    await run_appeal(cb.message, bot, cb.from_user.id, data, Evidence())


@dp.message(AppealFlow.evidence)
async def on_appeal_evidence(msg: Message, bot: Bot, state: FSMContext):
    gid = msg.media_group_id
    if gid:  # альбом: ждём остальные фото, берём первое изображение и все подписи
        if gid in appeal_albums:
            appeal_albums[gid].append(msg)
            return
        appeal_albums[gid] = [msg]
        await asyncio.sleep(ALBUM_WAIT)
        msgs = appeal_albums.pop(gid)
    else:
        msgs = [msg]
    if any(m.video or m.video_note or m.voice or m.audio for m in msgs):
        await msg.answer("Для апелляции пришли ссылку, текст, цитату или скриншот. Видео и голосовые здесь не принимаются.")
        return
    data = await state.get_data()
    await state.clear()
    text = "\n".join(m.text or m.caption or "" for m in msgs).strip()
    image = None
    for m in msgs:
        if m.photo:
            image = ("скриншот", (await bot.download(m.photo[-1])).read())
        elif m.document and (m.document.mime_type or "").startswith("image/"):
            image = ("скриншот", await media.to_jpeg((await bot.download(m.document)).read()))
        if image:
            break
    if not text and not image:
        await msg.answer("Не вижу ни текста, ни скриншота. Начни апелляцию заново кнопкой «⚖️ Оспорить».")
        return
    await run_appeal(msg, bot, msg.from_user.id, data, Evidence(text=text, image=image))


async def run_appeal(target: Message, bot: Bot, user_id: int, data: dict, ev: Evidence):
    key, idx = data["key"], data["idx"]
    status_msg: Message | None = None

    async def progress(status: str, appeal_id: int):
        nonlocal status_msg
        text = appeal_render.progress(status, appeal_id)
        if status == "accepted":
            await target.answer(text)
            status_msg = await target.answer("⏳ Начинаю перепроверку…")
            return
        try:
            await status_msg.edit_text(text)
        except (TelegramBadRequest, AttributeError):
            pass

    try:
        out = await appeals.submit(key, idx, user_id, data.get("reason", "oth"), ev, progress)
    except Exception:
        log.exception("Ошибка апелляции")
        text = "Не получилось завершить перепроверку. Статус обращения — в /appeals, попробуй ещё раз позже."
        if status_msg:
            await status_msg.edit_text(text)
        else:
            await target.answer(text)
        return

    if out.kind == "decided":
        card = appeal_render.card(out.decision, out.appeal_id)
        if status_msg:
            await status_msg.edit_text(card, reply_markup=after_appeal_keyboard(key), disable_web_page_preview=True)
        else:
            await target.answer(card, reply_markup=after_appeal_keyboard(key), disable_web_page_preview=True)
        await notify(bot, out.notify, target.chat.id, out.decision, out.appeal_id, key)
    elif out.kind == "repeat":
        await target.answer(appeal_render.repeat(out.message, out.previous_id),
                            reply_markup=open_appeal_keyboard(out.previous_id) if out.previous_id else None)
    else:  # refused, limit, stale
        text = out.message if out.kind == "refused" else f"⚠️ {out.message}"
        if status_msg:
            await status_msg.edit_text(text)
        else:
            await target.answer(text)


async def notify(bot: Bot, chats: list[int], skip: int, decision: dict, appeal_id: int, key: str):
    """Уведомляем тех, кто проверял материал раньше, что вердикт изменился."""
    text = appeal_render.notification(decision, appeal_id)
    for chat_id in chats:
        if chat_id == skip:
            continue
        try:
            await bot.send_message(chat_id, text, reply_markup=after_appeal_keyboard(key))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass  # пользователь заблокировал бота или удалил чат
        await asyncio.sleep(0.05)


@dp.callback_query(F.data.startswith("av:"))
async def on_appeal_view(cb: CallbackQuery):
    a = store.get(int(cb.data[3:]))
    if not a or not a.get("decision") or a["status"] != RESOLVED:
        await cb.answer("Решение по этому обращению не найдено.", show_alert=True)
        return
    await cb.message.answer(appeal_render.card(a["decision"], a["id"]), disable_web_page_preview=True)
    await cb.answer()


@dp.callback_query(F.data.startswith("h:"))
async def on_history(cb: CallbackQuery):
    key = cb.data[2:]
    result = checker.get(key)
    if not result:
        await cb.answer("Результат устарел, пришли материал ещё раз.", show_alert=True)
        return
    await cb.message.answer(appeal_render.history(result, store.versions(key)))
    await cb.answer()


# ---------------- проверка материала ----------------

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
        if med_docs.looks_like_medical_document(text):
            await status.edit_text(med_docs.REFUSAL)
            return
        key, result, cached = await checker.check(text, transcript, images)
        await status.edit_text(render.short(result, cached), reply_markup=keyboard(key), disable_web_page_preview=True)
        store.subscribe(key, first.chat.id)  # чтобы сообщить, если вердикт пересмотрят
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


async def purge_loop():
    """Срок хранения: раз в сутки обезличиваем старые доводы и удаляем старые подписки."""
    while True:
        try:
            store.purge()
        except Exception:
            log.exception("Ошибка очистки старых данных")
        await asyncio.sleep(DAY)


def telegram_session() -> AiohttpSession | None:
    """Обход блокировки api.telegram.org: свой адрес Bot API (например, Cloudflare Worker) или прокси."""
    if not (cfg.telegram_api_url or cfg.telegram_proxy):
        return None
    kwargs = {}
    if cfg.telegram_api_url:
        kwargs["api"] = TelegramAPIServer.from_base(cfg.telegram_api_url)
    if cfg.telegram_proxy:
        kwargs["proxy"] = cfg.telegram_proxy
    return AiohttpSession(**kwargs)


async def main():
    bot = Bot(cfg.telegram_token, session=telegram_session(),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    route = [name for name, on in (("свой адрес API", cfg.telegram_api_url), ("прокси", cfg.telegram_proxy)) if on]
    log.info("МедФильтр запущен. Распознавание речи: %s. Telegram: %s",
             "включено" if cfg.speech_enabled else "выключено", "через " + " и ".join(route) if route else "напрямую")
    purger = asyncio.create_task(purge_loop())
    try:
        await dp.start_polling(bot)
    finally:
        purger.cancel()
        await checker.llm.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
