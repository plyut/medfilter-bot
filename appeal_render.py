"""Оформление апелляций: карточка «было → стало», история версий, список обращений."""
from datetime import datetime

from .appeals import STATUS_LABELS, number
from .render import _cut, _num, _v, e
from .sources import link

OUTCOME_TITLES = {
    "confirmed": "✅ Вердикт подтверждён",
    "changed": "✏️ Вердикт изменён",
    "insufficient": "❔ Данных мало",
}


def _date(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y") if ts else ""


def _state(s: dict) -> str:
    icon, label = _v(s.get("verdict"))
    parts = [f"{icon} {label}"]
    udd, conf = _num(s.get("udd"), 1, 5), _num(s.get("confidence"), 0, 100)
    if udd:
        parts.append(f"УДД {udd}")
    if conf is not None:
        parts.append(f"уверенность {conf}%")
    return " · ".join(parts)


def choose_claim(result: dict) -> str:
    lines = ["⚖️ <b>Апелляция</b>", "", "Какое утверждение ты хочешь оспорить?", ""]
    for i, c in enumerate(result.get("claims") or [], 1):
        icon, label = _v(c.get("verdict"))
        lines.append(f"{i}. {e(c.get('claim'))} — {icon} {label.lower()}")
    return _cut("\n".join(lines))


def choose_reason(claim: dict) -> str:
    return _cut(f"⚖️ <b>Апелляция</b>\nУтверждение: <i>{e(claim.get('claim'))}</i>\n"
                f"Сейчас: {_state(claim)}\n\nС чем именно ты не согласен(а)?")


def ask_evidence(reason: str) -> str:
    return (f"Причина: <b>{e(reason)}</b>\n\n"
            "Пришли одним сообщением, на что опираешься:\n"
            "• ссылку на исследование, рекомендации или статью\n"
            "• текст или цитату\n"
            "• скриншот\n\n"
            "Можно оспорить и без источника, но тогда вердикт изменится, только если в нём есть ошибка.\n"
            "<i>Не присылай личные медицинские документы: выписки, анализы, рецепты.</i>")


PROGRESS = {
    "accepted": "📨 Апелляция <b>{n}</b> принята. Номер обращения сохранён, статус можно посмотреть в /appeals.",
    "source": "🔍 <b>{n}</b>: проверяю источник: доступность, автор, дата, тип…",
    "recheck": "⚖️ <b>{n}</b>: сравниваю с исходными источниками и перепроверяю вердикт…",
    "expert": "👩‍⚕️ <b>{n}</b>: случай спорный, передан эксперту. Пока вердикт помечен как «оспаривается»…",
}


def progress(status: str, appeal_id: int) -> str:
    return PROGRESS.get(status, "⏳ {n}").format(n=number(appeal_id))


def card(d: dict, appeal_id: int) -> str:
    """Ответ на апелляцию: было → стало, доводы, источник, что повлияло на решение."""
    outcome = d.get("outcome", "confirmed")
    lines = [f"⚖️ <b>Апелляция {number(appeal_id)} · решение</b>",
             f"Утверждение: <i>{e(d.get('claim'))}</i>", f"Причина: {e(d.get('reason'))}", "",
             f"<b>{OUTCOME_TITLES.get(outcome, OUTCOME_TITLES['confirmed'])}</b>",
             f"<b>Было:</b> {_state(d.get('before') or {})}",
             f"<b>Стало:</b> {_state(d.get('after') or {})}", ""]
    if d.get("admit_error"):
        lines += ["🙏 <b>Мы ошиблись.</b> Спасибо за поправку: вердикт исправлен.", ""]
    if d.get("explanation"):
        lines += [e(d["explanation"]), ""]
    if outcome == "insufficient" and d.get("what_needed"):
        lines += [f"<b>Каких данных не хватает:</b> {e(d['what_needed'])}", ""]

    args = d.get("arguments") or []
    if args:
        lines.append("<b>Твои доводы:</b>")
        for a in args:
            mark = "✅ учтён" if a.get("accepted") else "✖️ не принят"
            lines.append(f"• {e(a.get('argument'))} — {mark}" + (f": {e(a.get('why'))}" if a.get("why") else ""))
        lines.append("")

    src = d.get("source")
    if src:
        lines.append("<b>Новый источник:</b>")
        lines.append(f"• {e(src.get('title') or src.get('url'))}")
        meta = [e(src.get("kind_label")), e(src.get("status"))]
        meta.append("автор: " + (e(", ".join(src.get("authors") or [])) or "не указан"))
        meta.append("дата: " + (e(src.get("published")) or "не найдена"))
        lines.append("• " + " · ".join(m for m in meta if m))
        if src.get("kind") in ("blog", "ads"):
            lines.append("• <i>Блог, соцсеть и реклама не основание для смены вердикта.</i>")
    elif d.get("no_source"):
        lines.append("<b>Новый источник:</b> не приложен.")
    if d.get("source_assessment"):
        lines.append(f"<b>Сравнение с исходными источниками:</b> {e(d['source_assessment'])}")
    if d.get("decisive"):
        lines.append(f"<b>Что повлияло на решение:</b> {e(d['decisive'])}")
    if d.get("expert"):
        lines.append("👩‍⚕️ <b>Случай рассмотрен экспертом.</b>" + (f" {e(d['expert_note'])}" if d.get("expert_note") else ""))

    links = [link(s.get("id"), s.get("query")) for s in d.get("sources") or [] if isinstance(s, dict)]
    links = [x for x in links if x][:4]
    if links:
        lines += ["", "<b>Источники проверки:</b>"] + [f'• <a href="{e(u)}">{e(n)}</a>' for n, u in links]
    if d.get("version"):
        lines += ["", f"<i>Вердикт обновлён: версия {d['version']}. Его увидят все, кто проверит этот материал, "
                      "а те, кто проверял раньше, получат уведомление.</i>"]
    return _cut("\n".join(lines).strip())


def repeat(message: str, previous_id: int | None) -> str:
    out = f"↩️ {e(message)}"
    if previous_id:
        out += f"\n\nПрежнее решение: <b>{number(previous_id)}</b>. Открыть его можно кнопкой ниже."
    return out


def history(result: dict, versions: list[dict]) -> str:
    lines = [f"🕓 <b>История вердикта: {e(result.get('title'))}</b>", ""]
    if not versions:
        lines.append("Вердикт по этому материалу ещё не пересматривался.")
        return "\n".join(lines)
    claims = result.get("claims") or []
    by_claim: dict[int, list[dict]] = {}
    for v in versions:
        by_claim.setdefault(v["claim_idx"], []).append(v)
    for idx, vs in sorted(by_claim.items()):
        name = claims[idx].get("claim") if idx < len(claims) and isinstance(claims[idx], dict) else f"№{idx + 1}"
        lines.append(f"<b>{idx + 1}. {e(name)}</b>")
        for v in vs:
            icon, label = _v(v.get("verdict"))
            src = f" · апелляция {number(v['appeal_id'])}" if v.get("appeal_id") else ""
            mark = " · ⚖️ спорный" if v.get("disputed") else ""
            lines.append(f"v{v['version']} · {_date(v.get('created'))} · {icon} {label}{mark}{src}")
            if v.get("reason"):
                lines.append(f"   <i>Основание: {e(v['reason'])}</i>")
        lines.append("")
    return _cut("\n".join(lines).strip())


def appeals_list(rows: list[dict]) -> str:
    if not rows:
        return "У тебя пока нет апелляций. Чтобы оспорить вердикт, нажми «⚖️ Оспорить» под результатом проверки."
    lines = ["📂 <b>Твои апелляции</b>", ""]
    for a in rows:
        claim = (a.get("decision") or {}).get("claim") or ""
        outcome = (a.get("decision") or {}).get("outcome")
        res = f" · {OUTCOME_TITLES[outcome]}" if outcome in OUTCOME_TITLES else ""
        lines.append(f"<b>{number(a['id'])}</b> · {_date(a.get('created'))} · {STATUS_LABELS.get(a['status'], a['status'])}{res}")
        if claim:
            lines.append(f"   <i>{e(claim)}</i>")
    return _cut("\n".join(lines))


def notification(d: dict, appeal_id: int) -> str:
    return _cut(
        f"🔔 <b>Вердикт обновлён</b>\nМатериал, который ты присылал(а) на проверку: <i>{e(d.get('title'))}</i>\n\n"
        f"Утверждение: <i>{e(d.get('claim'))}</i>\n"
        f"Было: {_state(d.get('before') or {})}\nСтало: {_state(d.get('after') or {})}\n\n"
        f"Основание: апелляция {number(appeal_id)}." + (f" {e(d.get('decisive'))}" if d.get("decisive") else ""))

