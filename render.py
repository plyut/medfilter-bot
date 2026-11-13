"""Оформление ответа для Telegram (HTML-разметка)."""
from html import escape

from .sources import link

VERDICTS = {
    "true": ("✅", "Подтверждено"),
    "mixed": ("⚠️", "Частично верно"),
    "false": ("❌", "Опровергнуто"),
    "none": ("❔", "Недостаточно данных"),
}
UDD = {1: "систематические обзоры РКИ", 2: "отдельные РКИ / когортные исследования",
       3: "нерандомизированные исследования", 4: "серии случаев", 5: "мнение экспертов"}
LIMIT = 4000

SELF_CHECK_STEPS = (
    "1. Найди конкретное утверждение: что именно обещают и кому.\n"
    "2. Проверь автора: есть ли профильное образование, не продаёт ли он что-то.\n"
    "3. Ищи первоисточник: клинические рекомендации Минздрава, позицию ВОЗ, научные статьи.\n"
    "4. Смотри на дату: медицинские данные устаревают.\n"
    "5. Насторожись, если видишь «врачи скрывают», «100% результат», «только сегодня».\n"
    "6. Одна история пациента — не доказательство. Нужны исследования на многих людях."
)


def _v(verdict) -> tuple[str, str]:
    return VERDICTS.get(str(verdict), VERDICTS["none"])


def _num(x, lo, hi):
    try:
        return max(lo, min(hi, round(float(x))))
    except (TypeError, ValueError):
        return None


def _list(x) -> list:
    return x if isinstance(x, list) else []


def _cut(s: str) -> str:
    return s if len(s) <= LIMIT else s[: LIMIT - 1] + "…"


def e(x) -> str:
    return escape(str(x or ""))


def _marks(c: dict) -> str:
    """Пометки этапа 2: утверждение оспаривается или вердикт пересмотрен по апелляции."""
    if c.get("disputed"):
        return " · ⚖️ <i>оспаривается</i>"
    if c.get("revised"):
        return f" · <i>пересмотрено (А-{int(c['revised']):05d})</i>" if str(c["revised"]).isdigit() else " · <i>пересмотрено</i>"
    return ""


def short(r: dict, cached: bool = False) -> str:
    o = r.get("overall") or {}
    icon, label = _v(o.get("verdict"))
    score = _num(o.get("score"), 0, 100)
    lines = [f"🔎 <b>{e(r.get('title') or 'Результат проверки')}</b>",
             f"{icon} <b>{label}</b>" + (f" · достоверность {score}/100" if score is not None else ""), ""]
    if r.get("summary"):
        lines += [e(r["summary"]), ""]
    if r.get("is_medical") is False:
        lines += ["ℹ️ Материал не похож на медицинский. Я проверяю заявления о здоровье, лечении и профилактике.", ""]
    claims = _list(r.get("claims"))
    if claims:
        lines.append("<b>Утверждения:</b>")
        for i, c in enumerate(claims, 1):
            ci, cl = _v(c.get("verdict"))
            lines.append(f"{i}. {e(c.get('claim'))} — {ci} {cl.lower()}" + _marks(c))
        lines.append("")
    signs = _list(r.get("manipulation"))
    if signs:
        lines.append("⚠️ <b>Признаки манипуляции:</b> " + "; ".join(e(s.get("sign")) for s in signs if isinstance(s, dict)))
    if o.get("explanation"):
        lines += ["", f"<i>{e(o['explanation'])}</i>"]
    if claims:
        lines += ["", "<i>Есть возражения к вердикту? Нажми «⚖️ Оспорить».</i>"]
    if cached:
        lines += ["", "<i>Этот материал уже проверяли недавно, показываю сохранённый результат.</i>"]
    return _cut("\n".join(lines).strip())


def details(r: dict) -> str:
    lines = [f"📋 <b>Подробный разбор: {e(r.get('title'))}</b>", ""]
    for i, c in enumerate(_list(r.get("claims")), 1):
        ci, cl = _v(c.get("verdict"))
        lines.append(f"<b>{i}. {e(c.get('claim'))}</b>")
        lines.append(f"{ci} {cl}" + _marks(c))
        if c.get("quote"):
            lines.append(f"<i>«{e(c['quote'])}»</i>")
        if c.get("hedge"):
            lines.append(f"Оговорка автора: {e(c['hedge'])}")
        if c.get("explanation"):
            lines.append(e(c["explanation"]))
        udd, conf = _num(c.get("udd"), 1, 5), _num(c.get("confidence"), 0, 100)
        meta = []
        if udd:
            meta.append(f"УДД {udd} ({UDD[udd]})")
        if conf is not None:
            meta.append(f"уверенность {conf}%")
        if meta:
            lines.append("<code>" + " · ".join(meta) + "</code>")
        lines.append("")
    noise = _list(r.get("filtered_out"))
    if noise:
        lines.append("🗑 <b>Отсеяно как ненужное:</b>")
        lines += [f"• {e(n)}" for n in noise]
        lines.append("")
    signs = [s for s in _list(r.get("manipulation")) if isinstance(s, dict)]
    if signs:
        lines.append("⚠️ <b>Признаки манипуляции:</b>")
        lines += [f"• {e(s.get('sign'))}" + (f": <i>«{e(s.get('quote'))}»</i>" if s.get("quote") else "") for s in signs]
        lines.append("")
    if r.get("advice"):
        lines.append(f"🩺 {e(r['advice'])}")
    return _cut("\n".join(lines).strip())


def sources(r: dict) -> str:
    lines = ["🔗 <b>Где проверить</b>", "Ссылки ведут на поиск по надёжным источникам.", ""]
    found = False
    for i, c in enumerate(_list(r.get("claims")), 1):
        links = [link(s.get("id"), s.get("query")) for s in _list(c.get("sources")) if isinstance(s, dict)]
        links = [l for l in links if l][:4]
        if not links:
            continue
        found = True
        lines.append(f"<b>{i}. {e(c.get('claim'))}</b>")
        lines += [f'• <a href="{e(url)}">{e(name)}</a>' for name, url in links]
        lines.append("")
    if not found:
        lines.append("Для этого материала источники не подобраны.")
    return _cut("\n".join(lines).strip())


def self_check(r: dict) -> str:
    lines = ["🧠 <b>Как проверить самому</b>", ""]
    for i, c in enumerate(_list(r.get("claims")), 1):
        if c.get("self_check"):
            lines.append(f"{i}. {e(c['self_check'])}")
    tips = _list(r.get("how_to_check"))
    if tips:
        lines += ["", "<b>В следующий раз обрати внимание:</b>"] + [f"• {e(t)}" for t in tips]
    lines += ["", "<b>6 шагов проверки любой медицинской новости:</b>", SELF_CHECK_STEPS]
    return _cut("\n".join(lines).strip())
