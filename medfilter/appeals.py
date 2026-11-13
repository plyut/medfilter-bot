"""Модуль апелляций (этап 2).

Путь апелляции — пять шагов:
1. Принять: привязать к утверждению, присвоить номер, сообщить «принято».
2. Проверить источник: доступность, автор, дата, тип. Блог и реклама не основание.
3. Сравнить с исходными источниками: та же группа людей, условия, актуальность.
4. Решить: подтверждён / изменён / данных мало. Спорные случаи и резкие смены — эксперту.
5. Ответить: «было → стало», доводы, источники. Новая версия — в историю и в кэш,
   тем, кто проверял материал раньше, — уведомление.

Защита: жалобы не продавливают (вердикт меняют только доказательства, нейросеть не видит
число жалоб), те же доводы получают ссылку на прежнее решение, лимит апелляций в сутки.
"""
import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import med_docs, source_check
from .appeal_prompt import APPEAL_SYSTEM, EXPERT_SYSTEM, build_appeal_prompt, build_expert_prompt
from .appeal_store import DAY, AppealStore
from .privacy import mask_pii
from .prompt import parse_json

log = logging.getLogger(__name__)

REASONS = {
    "src": "У меня есть другой источник",
    "ctx": "Не учтён контекст или оговорка автора",
    "old": "Данные устарели",
    "mis": "Утверждение понято неверно",
    "oth": "Другое",
}

# Статусы обращения
ACCEPTED, SOURCE, RECHECK, EXPERT, RESOLVED, REPEAT, REFUSED, FAILED = (
    "accepted", "source", "recheck", "expert", "resolved", "repeat", "refused", "failed")
STATUS_LABELS = {
    ACCEPTED: "📨 принята",
    SOURCE: "🔍 проверка источника",
    RECHECK: "⚖️ перепроверка",
    EXPERT: "👩‍⚕️ у эксперта · вердикт оспаривается",
    RESOLVED: "✅ решение принято",
    REPEAT: "↩️ повтор: ответ дан ранее",
    REFUSED: "🚫 отклонена",
    FAILED: "⚠️ ошибка проверки",
}
OUTCOMES = ("confirmed", "changed", "insufficient")
VERDICTS = ("true", "mixed", "false", "none")
SIMILAR = 0.75       # доля общих слов, начиная с которой доводы считаются теми же
MAX_PER_CLAIM = 3    # сколько раз один пользователь может оспорить одно утверждение


def number(appeal_id: int) -> str:
    return f"А-{appeal_id:05d}"


@dataclass
class Evidence:
    text: str = ""
    image: tuple[str, bytes] | None = None   # (название, JPEG)

    @property
    def url(self) -> str:
        return source_check.find_url(self.text)

    @property
    def has_source(self) -> bool:
        return bool(self.url or self.image)


@dataclass
class AppealOutcome:
    kind: str                       # decided | repeat | limit | refused | stale
    appeal_id: int | None = None
    previous_id: int | None = None  # для повтора: номер прежнего решения
    decision: dict = field(default_factory=dict)
    notify: list[int] = field(default_factory=list)  # chat_id тех, кому отправить уведомление
    message: str = ""


Progress = Callable[[str, int | None], Awaitable[None]]


async def _noop(status: str, appeal_id: int | None) -> None:
    return None


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zа-яё0-9]+", (text or "").lower()) if len(w) > 2}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _norm_url(url: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", url.lower()).split("#")[0]
    u = re.sub(r"[?&]utm_[^&]*", "", u)
    return u.rstrip("/?")


def fingerprint(reason: str, ev: Evidence) -> str:
    h = hashlib.sha256(reason.encode())
    h.update(b"\0" + _norm_url(ev.url).encode())
    h.update(b"\0" + (hashlib.sha256(ev.image[1]).digest() if ev.image else b""))
    h.update(b"\0" + " ".join(sorted(tokens(ev.text))).encode())
    return h.hexdigest()[:32]


def sharp_change(old: str, new: str) -> bool:
    return {old, new} == {"true", "false"}


def recompute_overall(result: dict, note: str) -> None:
    claims = [c for c in result.get("claims") or [] if isinstance(c, dict)]
    vs = [c.get("verdict") for c in claims]
    if not vs:
        return
    if all(v == "true" for v in vs):
        v = "true"
    elif all(v == "none" for v in vs):
        v = "none"
    elif "false" in vs and "true" not in vs:
        v = "false"  # пересмотр одного пункта не делает фейковый пост «частично верным»
    else:
        v = "mixed"
    overall = dict(result.get("overall") or {})
    overall.update(verdict=v, revised=True, explanation=note)
    overall.pop("score", None)  # прежний балл посчитан для старых вердиктов
    result["overall"] = overall


def normalize(raw: dict, old: dict) -> dict:
    """Приводит ответ нейросети к допустимым значениям и проверяет согласованность итога."""
    d = dict(raw)
    old_v = str(old.get("verdict") or "none")
    outcome = str(d.get("outcome") or "").lower()
    verdict = str(d.get("verdict") or "").lower()
    if outcome not in OUTCOMES:
        outcome = "confirmed"
    if verdict not in VERDICTS:
        verdict = old_v
    if outcome == "confirmed":
        verdict = old_v
    elif outcome == "insufficient":
        verdict = "none"
    elif outcome == "changed" and verdict == old_v:
        outcome = "confirmed"  # «изменён» без смены вердикта — это подтверждение
    if outcome == "insufficient" and old_v == "none":
        outcome = "confirmed"
    d["outcome"], d["verdict"] = outcome, verdict
    try:
        d["confidence"] = max(0, min(100, int(round(float(d.get("confidence"))))))
    except (TypeError, ValueError):
        d["confidence"] = None
    try:
        d["udd"] = max(1, min(5, int(round(float(d.get("udd"))))))
    except (TypeError, ValueError):
        d["udd"] = old.get("udd")
    d["arguments"] = [a for a in d.get("arguments") or [] if isinstance(a, dict)]
    d["sources"] = [s for s in d.get("sources") or [] if isinstance(s, dict)]
    d["admit_error"] = bool(d.get("admit_error")) and outcome == "changed"
    d["needs_expert"] = bool(d.get("needs_expert"))
    for k in ("explanation", "source_assessment", "decisive", "what_needed", "expert_note"):
        d[k] = str(d.get(k) or "").strip()
    return d


class AppealService:
    def __init__(self, checker, store: AppealStore, salt: str, appeals_per_day: int = 5):
        self.checker = checker
        self.store = store
        self.salt = salt
        self.per_day = appeals_per_day
        self._locks: dict[str, asyncio.Lock] = {}

    def user_hash(self, user_id: int) -> str:
        """Анонимный ID: по нему нельзя восстановить Telegram ID без секрета бота."""
        return hashlib.sha256(f"{self.salt}:{user_id}".encode()).hexdigest()[:20]

    # ---------- шаг 0: можно ли принимать ----------
    def _previous_same(self, key: str, idx: int, fp: str, ev: Evidence) -> dict | None:
        """Ищет решённую апелляцию с теми же доводами по этому утверждению."""
        mine = tokens(ev.text)
        url = _norm_url(ev.url)
        for a in reversed(self.store.for_claim(key, idx)):
            if a["status"] != RESOLVED:
                continue
            if a["fingerprint"] == fp:
                return a
            same_source = (not ev.image) and (_norm_url(a.get("url") or "") == url)
            if same_source and a.get("tokens") and len(mine) >= 4 \
                    and similarity(mine, set(a["tokens"].split())) >= SIMILAR:
                return a
        return None

    # ---------- основной путь ----------
    async def submit(self, key: str, idx: int, user_id: int, reason_code: str, ev: Evidence,
                     progress: Progress = _noop) -> AppealOutcome:
        result = self.checker.get(key)
        claims = (result or {}).get("claims") or []
        if not result or not (0 <= idx < len(claims)) or not isinstance(claims[idx], dict):
            return AppealOutcome("stale", message="Результат проверки устарел. Пришли материал ещё раз.")
        reason = REASONS.get(reason_code, REASONS["oth"])
        uh = self.user_hash(user_id)

        if med_docs.looks_like_medical_document(ev.text):
            return AppealOutcome("refused", message=med_docs.REFUSAL)

        fp = fingerprint(reason, ev)
        prev = self._previous_same(key, idx, fp, ev)
        if prev:
            return AppealOutcome("repeat", previous_id=prev["id"], decision=prev["decision"] or {},
                                 message="Эти доводы уже рассматривались. Вердикт не меняется от повторных жалоб, "
                                         "его меняют только новые доказательства.")
        mine = [a for a in self.store.for_claim(key, idx) if a["user_hash"] == uh and a["status"] == RESOLVED]
        if mine and (len(mine) >= MAX_PER_CLAIM or not ev.has_source):
            return AppealOutcome("repeat", previous_id=mine[-1]["id"], decision=mine[-1]["decision"] or {},
                                 message="По этому утверждению у тебя уже есть решение. Повторная апелляция "
                                         "принимается только с новым источником: ссылкой или скриншотом."
                                 if len(mine) < MAX_PER_CLAIM else
                                 "По этому утверждению у тебя уже несколько решений. Решение остаётся в силе.")
        if self.store.count_since(uh, DAY) >= self.per_day:
            return AppealOutcome("limit", message=f"Сегодня от тебя уже {self.per_day} апелляций — это лимит на сутки. "
                                                  "Попробуй завтра — так сервис защищён от накруток.")

        argument = mask_pii(ev.text.strip())
        appeal_id = self.store.create(key, idx, uh, reason, argument, ev.url, fp,
                                      " ".join(sorted(tokens(argument))), ACCEPTED)
        await progress(ACCEPTED, appeal_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                return await self._process(appeal_id, key, idx, reason, argument, ev, progress)
        except Exception:
            self.store.set_status(appeal_id, FAILED)
            self._set_disputed(key, idx, False)
            raise

    async def _process(self, appeal_id: int, key: str, idx: int, reason: str, argument: str, ev: Evidence,
                       progress: Progress) -> AppealOutcome:
        llm, slots = self.checker.llm, self.checker.slots

        # Скриншот: извлекаем текст. Медицинские документы не принимаем.
        screenshot = ""
        if ev.image:
            async with slots:
                screenshot = mask_pii(await llm.describe(*ev.image))
            if med_docs.looks_like_medical_document(screenshot):
                self.store.finish(appeal_id, REFUSED, None, {"refused": "medical_document"})
                self.store.scrub(appeal_id)  # ничего из документа не сохраняем
                return AppealOutcome("refused", appeal_id=appeal_id, message=med_docs.REFUSAL)

        # Шаг 2: проверка источника
        info = None
        if ev.url:
            self.store.set_status(appeal_id, SOURCE)
            await progress(SOURCE, appeal_id)
            info = await source_check.check_url(ev.url)
            info.excerpt = mask_pii(info.excerpt)

        # Шаги 3–4: перепроверка
        self.store.set_status(appeal_id, RECHECK)
        await progress(RECHECK, appeal_id)
        result = self.checker.get(key)
        claim = dict(result["claims"][idx])
        prompt = build_appeal_prompt(result.get("title", ""), claim, reason, argument,
                                     source_check.describe_for_prompt(info) if info else "", screenshot)
        decision = normalize(await self._ask(APPEAL_SYSTEM, prompt), claim)

        old_v = str(claim.get("verdict") or "none")
        reliable = bool(info and info.reliable and info.accessible)
        to_expert = (decision["needs_expert"]
                     or (decision["outcome"] == "changed" and (sharp_change(old_v, decision["verdict"]) or not reliable))
                     or (decision["outcome"] != "confirmed" and (decision["confidence"] or 0) < 60))
        expert = False
        if to_expert:
            self.store.set_status(appeal_id, EXPERT)
            self._set_disputed(key, idx, True)
            await progress(EXPERT, appeal_id)
            decision = normalize(await self._ask(EXPERT_SYSTEM, build_expert_prompt(prompt, decision), 0.1), claim)
            expert = True

        # Шаг 5: новая версия вердикта, кэш, уведомления
        result = self.checker.get(key) or result
        claim_now = dict(result["claims"][idx])
        before = {k: claim.get(k) for k in ("verdict", "confidence", "udd", "explanation")}
        notify: list[int] = []
        version = None
        self.store.ensure_first_version(key, idx, claim)
        if decision["verdict"] != old_v:
            claim_now.update(verdict=decision["verdict"], explanation=decision["explanation"] or claim.get("explanation"),
                             confidence=decision["confidence"], udd=decision["udd"], revised=appeal_id)
            if decision["sources"]:
                claim_now["sources"] = decision["sources"]
            claim_now.pop("disputed", None)
            result["claims"][idx] = claim_now
            recompute_overall(result, f"Вердикт по утверждению {idx + 1} пересмотрен после апелляции {number(appeal_id)}.")
            self.checker.cache.put(key, result)
            version = self.store.add_version(key, idx, claim_now, decision["decisive"] or decision["explanation"],
                                             appeal_id, disputed=decision["outcome"] == "insufficient")
            notify = self.store.subscribers(key)
        else:
            self._set_disputed(key, idx, False)
        after = {"verdict": decision["verdict"], "confidence": decision["confidence"], "udd": decision["udd"],
                 "explanation": decision["explanation"]}
        decision.update(claim=claim.get("claim"), title=result.get("title"), reason=reason, before=before,
                        after=after, expert=expert, version=version, key=key, idx=idx,
                        source=info.to_dict() if info else None, screenshot=bool(ev.image),
                        no_source=not (ev.url or ev.image))
        self.store.finish(appeal_id, RESOLVED, decision["outcome"], decision)
        return AppealOutcome("decided", appeal_id=appeal_id, decision=decision, notify=notify)

    async def _ask(self, system: str, prompt: str, temperature: float = 0.2) -> dict:
        async with self.checker.slots:
            reply = await self.checker.llm.ask_as(system, prompt, temperature)
            try:
                return parse_json(reply)
            except ValueError:
                log.info("Ответ апелляции не JSON, повторяю запрос один раз")
                return parse_json(await self.checker.llm.ask_as(system, prompt + "\n\nВерни ответ строго в формате JSON.",
                                                                temperature))

    def _set_disputed(self, key: str, idx: int, value: bool) -> None:
        """Пометка «оспаривается» видна всем, кто откроет результат, пока дело у эксперта."""
        result = self.checker.get(key)
        if not result:
            return
        claims = result.get("claims") or []
        if not (0 <= idx < len(claims)) or not isinstance(claims[idx], dict):
            return
        if bool(claims[idx].get("disputed")) == value:
            return
        if value:
            claims[idx]["disputed"] = True
        else:
            claims[idx].pop("disputed", None)
        self.checker.cache.put(key, result)
