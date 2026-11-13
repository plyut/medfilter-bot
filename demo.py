"""Демо-сценарии апелляций на вымышленных пользователях (без Telegram и без нейросети).

Запуск: python demo.py
Показывает, что увидит пользователь в каждом сценарии: номер обращения, статусы и
карточку «было → стало». Ответы нейросети заранее записаны, источник не загружается из сети.
Для скриншотов в Telegram пройдите те же сценарии вживую — см. docs/demo-scenarios.md.
"""
import asyncio
import copy
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from medfilter import appeal_render, render, source_check
from medfilter.appeal_store import AppealStore
from medfilter.appeals import AppealService, Evidence
from medfilter.checker import Checker
from medfilter.config import Config

MATERIAL = {
    "title": "Сода, витамин C и прививки",
    "summary": "Пост в канале о «натуральном здоровье» советует пить соду от рака, "
               "лечить простуду витамином C и отказаться от прививки от гриппа.",
    "is_medical": True,
    "overall": {"verdict": "false", "score": 15, "explanation": "Главные утверждения опровергнуты."},
    "claims": [
        {"quote": "сода убивает раковые клетки", "claim": "Пищевая сода лечит рак", "hedge": "", "verdict": "false",
         "confidence": 96, "udd": 1, "explanation": "Исследований, показывающих, что сода лечит рак у людей, нет.",
         "sources": [{"id": "cancer", "query": "sodium bicarbonate cancer treatment"}]},
        {"quote": "витамин C вылечит простуду за день", "claim": "Витамин C лечит простуду", "hedge": "",
         "verdict": "false", "confidence": 85, "udd": 1,
         "explanation": "Витамин C не лечит простуду.", "sources": [{"id": "cochrane", "query": "vitamin C common cold"}]},
        {"quote": "прививка от гриппа бесполезна", "claim": "Прививка от гриппа бесполезна", "hedge": "",
         "verdict": "false", "confidence": 95, "udd": 1, "explanation": "Вакцинация снижает риск гриппа и осложнений.",
         "sources": [{"id": "who", "query": "influenza vaccine effectiveness"}]},
    ],
}
KEY = "demo00000000000000000000"


def reply(**d) -> str:
    base = {"arguments": [], "source_assessment": "", "decisive": "", "admit_error": False, "what_needed": "",
            "sources": [], "needs_expert": False, "udd": 1, "confidence": 85}
    base.update(d)
    return json.dumps(base, ensure_ascii=False)


SCENARIOS = [
    {
        "who": "Маша, 15 лет: прислала ссылку на блог «врача-натуропата»",
        "claim": 0, "reason": "src", "user": 1001,
        "evidence": "Вот, тут врач пишет, что сода помогает! https://dzen.ru/a/soda-protiv-raka",
        "source": source_check.SourceInfo("https://dzen.ru/a/soda-protiv-raka", True, "доступна",
                                          "Сода — дешёвое средство, которое скрывают", [], "", None, "blog"),
        "replies": [reply(outcome="confirmed", verdict="false", explanation=(
            "Статья в блоге не опровергает вывод: в ней нет исследований на людях, только пересказ опытов "
            "в пробирке и личные истории."),
            arguments=[{"argument": "врач пишет, что сода помогает", "accepted": False,
                        "why": "это мнение автора блога без ссылок на исследования"}],
            source_assessment="Блог, автор не указан, дата не указана. Опыты в пробирке — другая ситуация, "
                              "не лечение людей.",
            decisive="В клинических рекомендациях и обзорах сода как лечение рака не упоминается.",
            sources=[{"id": "cancer", "query": "sodium bicarbonate cancer"}])],
    },
    {
        "who": "Артём, 16 лет: нашёл обзор Cochrane про витамин C",
        "claim": 1, "reason": "src", "user": 1002,
        "evidence": "Вы слишком строго: в обзоре Cochrane витамин C немного сокращает простуду "
                    "https://www.cochranelibrary.com/cdsr/doi/10.1002/14651858.CD000980.pub4/full",
        "source": source_check.SourceInfo("https://www.cochranelibrary.com/cdsr/doi/10.1002/14651858.CD000980.pub4/full",
                                          True, "доступна", "Vitamin C for preventing and treating the common cold",
                                          ["Hemilä H", "Chalker E"], "2013-01-31", 13.8, "science"),
        "replies": [reply(outcome="changed", verdict="mixed", confidence=80, admit_error=True, explanation=(
            "Мы ошиблись в формулировке: витамин C не вылечит простуду за день, но при регулярном приёме "
            "немного сокращает её длительность (примерно на 8% у взрослых). Поэтому утверждение частично верно."),
            arguments=[{"argument": "обзор Cochrane показывает небольшой эффект", "accepted": True,
                        "why": "это систематический обзор РКИ, он прямо относится к утверждению"}],
            source_assessment="Систематический обзор, та же группа людей (взрослые и дети с простудой). "
                              "Опубликован в 2013 г., но новых обзоров, которые бы ему противоречили, нет.",
            decisive="Систематический обзор РКИ надёжнее нашего первоначального вывода.",
            sources=[{"id": "cochrane", "query": "vitamin C common cold"}])],
    },
    {
        "who": "Игорь, 17 лет: оспаривает без источника, «у меня после прививки был грипп»",
        "claim": 2, "reason": "oth", "user": 1003,
        "evidence": "Я делал прививку и всё равно заболел гриппом, значит она бесполезна",
        "source": None,
        "replies": [reply(outcome="changed", verdict="mixed", confidence=55, needs_expert=True, explanation="..."),
                    reply(outcome="confirmed", verdict="false", confidence=93, explanation=(
                        "Прививка не даёт стопроцентной защиты, но снижает риск заболеть и тяжёлых осложнений. "
                        "Один случай болезни после прививки не опровергает данные исследований на миллионах людей."),
                        arguments=[{"argument": "заболел после прививки", "accepted": False,
                                    "why": "личный опыт не показывает, как было бы без прививки"}],
                        decisive="Систематические обзоры и позиция ВОЗ.",
                        expert_note="Предварительное решение отклонено: личная история не основание для смены вердикта.",
                        sources=[{"id": "who", "query": "influenza vaccine effectiveness"}])],
    },
    {
        "who": "Саша, 14 лет: повторяет жалобу Игоря другими словами",
        "claim": 2, "reason": "oth", "user": 1004,
        "evidence": "я сделал прививку и всё равно заболел гриппом значит она бесполезна",
        "source": None, "replies": [],
    },
]


def plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


class ScriptedLLM:
    def __init__(self):
        self.queue: list[str] = []

    async def describe(self, name, data):
        return ""

    async def ask_as(self, system, prompt, temperature=0.2):
        return self.queue.pop(0)


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config("demo", "demo", "s", "m", False, "", "", 1, 30, Path(tmp))
        llm = ScriptedLLM()
        checker = Checker(cfg, llm=llm)
        checker.cache.put(KEY, copy.deepcopy(MATERIAL))
        store = AppealStore(Path(tmp) / "appeals.sqlite3")
        service = AppealService(checker, store, salt="demo")
        for chat in (1001, 1002, 1003, 1004, 2001):
            store.subscribe(KEY, chat)

        print("=" * 70, "\nИСХОДНЫЙ РЕЗУЛЬТАТ ПРОВЕРКИ\n" + "=" * 70)
        print(plain(render.short(checker.get(KEY))))

        for sc in SCENARIOS:
            print("\n" + "=" * 70 + f"\nСЦЕНАРИЙ: {sc['who']}\n" + "=" * 70)
            llm.queue = list(sc["replies"])

            async def progress(status, aid):
                print("  →", plain(appeal_render.progress(status, aid)))

            async def fake_check(url, _info=sc["source"]):
                return _info

            with patch("medfilter.source_check.check_url", new=fake_check):
                out = await service.submit(KEY, sc["claim"], sc["user"], sc["reason"], Evidence(sc["evidence"]), progress)
            if out.kind == "decided":
                print(plain(appeal_render.card(out.decision, out.appeal_id)))
                others = [c for c in out.notify if c != sc["user"]]
                if others:
                    print(f"\n  🔔 Уведомление отправлено ещё {len(others)} пользователям:")
                    print("  " + plain(appeal_render.notification(out.decision, out.appeal_id)).replace("\n", "\n  "))
            elif out.kind == "repeat":
                print(plain(appeal_render.repeat(out.message, out.previous_id)))
            else:
                print(plain(out.message))

        print("\n" + "=" * 70 + "\nИТОГ: РЕЗУЛЬТАТ ПОСЛЕ АПЕЛЛЯЦИЙ И ИСТОРИЯ\n" + "=" * 70)
        print(plain(render.short(checker.get(KEY))))
        print()
        print(plain(appeal_render.history(checker.get(KEY), store.versions(KEY))))
        print()
        print(plain(appeal_render.appeals_list(store.by_user(service.user_hash(1002)))))
        checker.cache.db.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
