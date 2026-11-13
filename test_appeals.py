"""Тесты модуля апелляций (этап 2). Нейросеть и сеть заменены заглушками."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from medfilter import appeal_render, med_docs, render, source_check
from medfilter.appeal_store import AppealStore
from medfilter.appeals import AppealService, Evidence, normalize
from medfilter.checker import Checker
from medfilter.config import Config

SAMPLE = {
    "title": "Сода против рака",
    "summary": "Пост советует пить соду.",
    "is_medical": True,
    "overall": {"verdict": "false", "score": 10, "explanation": "Опровергнуто."},
    "claims": [
        {"quote": "сода убивает рак", "claim": "Сода лечит рак", "hedge": "", "verdict": "false",
         "confidence": 95, "udd": 1, "explanation": "Нет доказательств.", "sources": [{"id": "pubmed", "query": "soda cancer"}]},
        {"quote": "вода полезна", "claim": "Пить воду полезно", "hedge": "", "verdict": "true",
         "confidence": 90, "udd": 2, "explanation": "Да.", "sources": []},
    ],
}
KEY = "k" * 24


def decision(outcome, verdict, conf=80, needs_expert=False, **extra):
    d = {"outcome": outcome, "verdict": verdict, "confidence": conf, "udd": 2, "explanation": "Пояснение.",
         "arguments": [{"argument": "есть исследование", "accepted": outcome == "changed", "why": "потому что"}],
         "source_assessment": "Та же группа людей.", "decisive": "Новый обзор.", "admit_error": False,
         "what_needed": "", "sources": [{"id": "cochrane", "query": "bicarbonate"}], "needs_expert": needs_expert}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


class FakeLLM:
    def __init__(self, replies, describe="Скриншот статьи о соде"):
        self.replies, self.calls, self.systems, self._describe = list(replies), 0, [], describe

    async def describe(self, name, data):
        return self._describe

    async def ask_as(self, system, prompt, temperature=0.2):
        self.calls += 1
        self.systems.append(system)
        self.last_prompt = prompt
        return self.replies.pop(0)


def make(tmp, replies, per_day=5, describe="Скриншот статьи о соде"):
    cfg = Config("t", "c", "s", "m", False, "", "", 1, 30, Path(tmp))
    llm = FakeLLM(replies, describe)
    ch = Checker(cfg, llm=llm)
    ch.cache.put(KEY, copy.deepcopy(SAMPLE))
    store = AppealStore(Path(tmp) / "a.sqlite3", 180)
    return AppealService(ch, store, "salt", per_day), ch, store, llm


def reliable_source(url):
    async def fake(u):
        return source_check.SourceInfo(url=u, accessible=True, status="доступна", title="Cochrane review",
                                       authors=["Ivanov I."], published="2024-05-01", age_years=1.5, kind="science")
    return patch("medfilter.source_check.check_url", new=fake)


def close(ch, store):
    ch.cache.db.close()
    store.close()


class AppealFlowTest(unittest.TestCase):
    def test_confirmed_without_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [decision("confirmed", "true")])
            seen = []

            async def progress(status, aid):
                seen.append(status)

            out = asyncio.run(svc.submit(KEY, 0, 1, "mis", Evidence(), progress))
            self.assertEqual(out.kind, "decided")
            self.assertEqual(out.decision["outcome"], "confirmed")
            self.assertEqual(out.decision["after"]["verdict"], "false")  # confirmed = прежний вердикт
            self.assertEqual(seen, ["accepted", "recheck"])
            self.assertEqual(ch.get(KEY)["claims"][0]["verdict"], "false")
            self.assertEqual(store.versions(KEY, 0)[0]["reason"], "первичная проверка")
            self.assertEqual(len(store.versions(KEY, 0)), 1)
            card = appeal_render.card(out.decision, out.appeal_id)
            self.assertIn("А-00001", card)
            self.assertIn("Было:", card)
            self.assertIn("Вердикт подтверждён", card)
            self.assertIn("не приложен", card)
            close(ch, store)

    def test_changed_with_reliable_source_updates_cache_history_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [decision("changed", "mixed", admit_error=True)])
            store.subscribe(KEY, 111)
            store.subscribe(KEY, 222)
            with reliable_source("https://cochranelibrary.com/x"):
                out = asyncio.run(svc.submit(KEY, 0, 1, "src", Evidence("См. https://cochranelibrary.com/x")))
            self.assertEqual(llm.calls, 1)  # не резкая смена и надёжный источник: эксперт не нужен
            self.assertFalse(out.decision["expert"])
            claim = ch.get(KEY)["claims"][0]
            self.assertEqual(claim["verdict"], "mixed")
            self.assertEqual(claim["revised"], out.appeal_id)
            self.assertEqual(ch.get(KEY)["overall"]["verdict"], "mixed")
            self.assertNotIn("score", ch.get(KEY)["overall"])
            self.assertEqual([v["verdict"] for v in store.versions(KEY, 0)], ["false", "mixed"])
            self.assertEqual(sorted(out.notify), [111, 222])
            self.assertIn("Мы ошиблись", appeal_render.card(out.decision, out.appeal_id))
            self.assertIn("пересмотрено", render.short(ch.get(KEY)))
            self.assertIn("v2", appeal_render.history(ch.get(KEY), store.versions(KEY)))
            self.assertIn("Вердикт обновлён", appeal_render.notification(out.decision, out.appeal_id))
            self.assertIn("Ivanov", appeal_render.card(out.decision, out.appeal_id))
            close(ch, store)

    def test_sharp_change_goes_to_expert(self):
        with tempfile.TemporaryDirectory() as tmp:
            expert = decision("confirmed", "false", expert_note="Источник о другой группе.")
            svc, ch, store, llm = make(tmp, [decision("changed", "true"), expert])
            seen = []

            async def progress(status, aid):
                seen.append(status)
                if status == "expert":  # пока дело у эксперта, вердикт помечен «оспаривается»
                    self.assertTrue(ch.get(KEY)["claims"][0]["disputed"])
                    self.assertIn("оспаривается", render.short(ch.get(KEY)))

            with reliable_source("https://pubmed.ncbi.nlm.nih.gov/1"):
                out = asyncio.run(svc.submit(KEY, 0, 1, "src", Evidence("https://pubmed.ncbi.nlm.nih.gov/1"), progress))
            self.assertEqual(seen, ["accepted", "source", "recheck", "expert"])
            self.assertEqual(llm.calls, 2)
            self.assertIn("врач-эксперт", llm.systems[1])
            self.assertTrue(out.decision["expert"])
            self.assertEqual(out.decision["outcome"], "confirmed")
            self.assertEqual(ch.get(KEY)["claims"][0]["verdict"], "false")
            self.assertNotIn("disputed", ch.get(KEY)["claims"][0])
            self.assertIn("рассмотрен экспертом", appeal_render.card(out.decision, out.appeal_id))
            close(ch, store)

    def test_change_without_reliable_source_goes_to_expert(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [decision("changed", "mixed"), decision("insufficient", "none",
                                                                                   what_needed="Нужны РКИ.")])
            out = asyncio.run(svc.submit(KEY, 0, 1, "ctx", Evidence("автор говорил только про изжогу")))
            self.assertEqual(llm.calls, 2)
            self.assertEqual(out.decision["outcome"], "insufficient")
            self.assertEqual(ch.get(KEY)["claims"][0]["verdict"], "none")
            self.assertIn("Каких данных не хватает", appeal_render.card(out.decision, out.appeal_id))
            close(ch, store)

    def test_same_arguments_get_previous_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [decision("confirmed", "false")])
            text = "Мой дедушка пил соду и вылечился от рака, это доказано"
            first = asyncio.run(svc.submit(KEY, 0, 1, "oth", Evidence(text)))
            again = asyncio.run(svc.submit(KEY, 0, 2, "oth", Evidence(text.upper() + "!!!")))
            similar = asyncio.run(svc.submit(KEY, 0, 3, "oth", Evidence("мой дедушка пил соду и вылечился от рака доказано")))
            self.assertEqual(again.kind, "repeat")
            self.assertEqual(again.previous_id, first.appeal_id)
            self.assertEqual(similar.kind, "repeat")
            self.assertEqual(llm.calls, 1)  # повторные жалобы не уходят в нейросеть
            self.assertIn("А-00001", appeal_render.repeat(again.message, again.previous_id))
            close(ch, store)

    def test_same_user_needs_new_source_to_appeal_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [decision("confirmed", "false"), decision("confirmed", "false")])
            asyncio.run(svc.submit(KEY, 0, 1, "oth", Evidence("не согласен")))
            out = asyncio.run(svc.submit(KEY, 0, 1, "mis", Evidence("всё равно не согласен, вы неправы")))
            self.assertEqual(out.kind, "repeat")
            with reliable_source("https://who.int/a"):
                out2 = asyncio.run(svc.submit(KEY, 0, 1, "src", Evidence("https://who.int/a")))
            self.assertEqual(out2.kind, "decided")
            close(ch, store)

    def test_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [decision("confirmed", "false")], per_day=1)
            asyncio.run(svc.submit(KEY, 0, 1, "oth", Evidence("довод один")))
            out = asyncio.run(svc.submit(KEY, 1, 1, "oth", Evidence("совсем другой довод про воду")))
            self.assertEqual(out.kind, "limit")
            close(ch, store)

    def test_medical_document_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [])
            doc = "Выписной эпикриз. Пациент: Иванов И. И., дата рождения 01.01.2010. Диагноз: J06.9"
            out = asyncio.run(svc.submit(KEY, 0, 1, "oth", Evidence(doc)))
            self.assertEqual(out.kind, "refused")
            self.assertEqual(store.for_claim(KEY, 0), [])
            self.assertEqual(llm.calls, 0)
            close(ch, store)

    def test_medical_document_screenshot_refused_and_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [], describe="Бланк: Результаты анализов. Референсные значения ... "
                                                          "Пациент: Петров. Лечащий врач: Сидоров")
            out = asyncio.run(svc.submit(KEY, 0, 1, "src", Evidence("вот мои анализы", ("скриншот", b"jpeg"))))
            self.assertEqual(out.kind, "refused")
            row = store.get(out.appeal_id)
            self.assertIsNone(row["argument"])
            self.assertEqual(row["status"], "refused")
            close(ch, store)

    def test_stale_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [])
            self.assertEqual(asyncio.run(svc.submit("missing", 0, 1, "oth", Evidence())).kind, "stale")
            self.assertEqual(asyncio.run(svc.submit(KEY, 9, 1, "oth", Evidence())).kind, "stale")
            close(ch, store)

    def test_failure_clears_disputed_and_marks_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, llm = make(tmp, [decision("changed", "true")])  # эксперту ответа нет → ошибка
            with self.assertRaises(IndexError):
                asyncio.run(svc.submit(KEY, 0, 1, "mis", Evidence("вы не поняли автора")))
            self.assertNotIn("disputed", ch.get(KEY)["claims"][0])
            self.assertEqual(store.by_user(svc.user_hash(1))[0]["status"], "failed")
            close(ch, store)


class OverallTest(unittest.TestCase):
    def test_recompute(self):
        from medfilter.appeals import recompute_overall
        cases = [(["false", "mixed", "false"], "false"), (["mixed", "true"], "mixed"),
                 (["true", "true"], "true"), (["none"], "none"), (["false", "true"], "mixed")]
        for vs, expected in cases:
            r = {"overall": {"score": 5}, "claims": [{"verdict": v} for v in vs]}
            recompute_overall(r, "note")
            self.assertEqual(r["overall"]["verdict"], expected, vs)


class NormalizeTest(unittest.TestCase):
    def test_consistency(self):
        old = {"verdict": "false", "udd": 1}
        self.assertEqual(normalize({"outcome": "changed", "verdict": "false"}, old)["outcome"], "confirmed")
        self.assertEqual(normalize({"outcome": "confirmed", "verdict": "true"}, old)["verdict"], "false")
        self.assertEqual(normalize({"outcome": "insufficient", "verdict": "true"}, old)["verdict"], "none")
        bad = normalize({"outcome": "???", "verdict": "??", "confidence": "много", "udd": 9}, old)
        self.assertEqual((bad["outcome"], bad["verdict"], bad["confidence"], bad["udd"]), ("confirmed", "false", None, 5))
        self.assertFalse(normalize({"outcome": "confirmed", "admit_error": True}, old)["admit_error"])


class SourceCheckTest(unittest.TestCase):
    def test_parse_scientific_page(self):
        html = """<html><head><title>Тест</title>
            <meta name="citation_title" content="Bicarbonate and cancer: a review">
            <meta name="citation_author" content="Smith J"><meta name="citation_author" content="Lee K">
            <meta name="citation_publication_date" content="2021/03/15"></head>
            <body><script>var x=1</script><p>Abstract text here.</p></body></html>"""
        info = source_check.parse_page("https://example.org/article", html, today=date(2026, 3, 15))
        self.assertEqual(info.kind, "science")
        self.assertEqual(info.authors, ["Smith J", "Lee K"])
        self.assertEqual(info.published, "2021-03-15")
        self.assertEqual(info.age_years, 5.0)
        self.assertIn("Abstract text", info.excerpt)
        self.assertNotIn("var x", info.excerpt)

    def test_json_ld_and_kinds(self):
        html = '<script type="application/ld+json">{"@type":"NewsArticle","author":{"name":"Анна"},' \
               '"datePublished":"2025-01-02T10:00:00"}</script><p>текст</p>'
        info = source_check.parse_page("https://ria.ru/x", html)
        self.assertEqual((info.kind, info.authors, info.published), ("media", ["Анна"], "2025-01-02"))
        self.assertEqual(source_check.classify("https://t.me/channel/5"), "blog")
        self.assertEqual(source_check.classify("https://www.ozon.ru/product/1"), "ads")
        self.assertEqual(source_check.classify("https://cr.minzdrav.gov.ru/rec/1"), "official")
        self.assertEqual(source_check.classify("https://shop.example/?utm_source=x"), "ads")
        self.assertTrue(source_check.SourceInfo("u", kind="blog").not_grounds)
        self.assertIn("не являются основанием",
                      source_check.describe_for_prompt(source_check.SourceInfo("u", kind="ads")))

    def test_find_url(self):
        self.assertEqual(source_check.find_url("см. (https://who.int/news/1)."), "https://who.int/news/1")
        self.assertEqual(source_check.find_url("без ссылки"), "")

    def test_internal_addresses_blocked(self):
        for url in ("http://127.0.0.1:8080/", "http://localhost/", "file:///etc/passwd", "http://10.0.0.1/"):
            info = asyncio.run(source_check.check_url(url))
            self.assertFalse(info.accessible)
            self.assertIn("не проверялась", info.status)


class PrivacyStage2Test(unittest.TestCase):
    def test_medical_document_detector(self):
        self.assertTrue(med_docs.looks_like_medical_document("Медицинская карта № 15. Дата рождения: 2009"))
        self.assertFalse(med_docs.looks_like_medical_document("Сдайте анализ крови, если устали: витамин D важен"))

    def test_delete_and_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AppealStore(Path(tmp) / "a.sqlite3", retention_days=180)
            aid = store.create(KEY, 0, "user1", "r", "мой довод", "https://x", "fp", "мой довод", "resolved")
            store.subscribe(KEY, 42)
            self.assertEqual(store.forget_user("user1", 42), (1, 1))
            row = store.get(aid)
            self.assertIsNone(row["argument"])
            self.assertIsNone(row["user_hash"])
            self.assertEqual(store.subscribers(KEY), [])
            aid2 = store.create(KEY, 0, "user2", "r", "старый довод", "", "fp2", "", "resolved")
            store.subscribe(KEY, 7)
            old = time.time() - 200 * 86400
            store.db.execute("UPDATE appeals SET created = ? WHERE id = ?", (old, aid2))
            store.db.execute("UPDATE subscribers SET created = ?", (old,))
            store.purge()
            self.assertIsNone(store.get(aid2)["argument"])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)
            store.close()

    def test_anonymous_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, ch, store, _ = make(tmp, [])
            h = svc.user_hash(123456)
            self.assertNotIn("123456", h)
            self.assertEqual(h, svc.user_hash(123456))
            close(ch, store)


if __name__ == "__main__":
    unittest.main()
