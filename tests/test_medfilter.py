import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from medfilter import media, render
from medfilter.cache import Cache, make_key
from medfilter.checker import Checker
from medfilter.config import Config
from medfilter.privacy import mask_pii
from medfilter.prompt import parse_json

SAMPLE = {
    "title": "Сода с лимоном против рака",
    "summary": "Пост рекламирует БАД и отговаривает от прививки.",
    "is_medical": True,
    "overall": {"verdict": "false", "score": 8, "explanation": "Главные утверждения опровергнуты."},
    "filtered_out": ["Реклама «АлкаЛайф»"],
    "manipulation": [{"sign": "Теория заговора", "quote": "фармкомпании скрывают"}],
    "claims": [{"quote": "убивает раковые клетки", "claim": "Сода лечит рак", "hedge": "", "verdict": "false",
                "confidence": 97, "udd": 1, "explanation": "Нет исследований на людях.",
                "sources": [{"id": "pubmed", "query": "sodium bicarbonate cancer"}, {"id": "fake", "query": "x"}],
                "self_check": "Спроси, где опубликовано исследование."}],
    "how_to_check": ["Ищи имя врача."],
    "advice": "Обсуди с онкологом.",
}


def cfg(tmp: str) -> Config:
    return Config("t", "c", "GIGACHAT_API_PERS", "GigaChat-2-Max", False, "", "", 2, 30, Path(tmp))


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    async def describe(self, name, data):
        return "ЧЕСНОК ЗАМЕНЯЕТ СТАТИНЫ, звоните 89001112233"

    async def ask(self, prompt):
        self.calls += 1
        self.last_prompt = prompt
        return self.replies.pop(0)


class PrivacyTest(unittest.TestCase):
    def test_masks(self):
        s = mask_pii("Пиши на ivan@mail.ru или +7 (912) 345-67-89, СНИЛС 123-456-789 01, паспорт 3623 381591")
        for leaked in ("ivan@mail.ru", "345-67-89", "123-456-789", "381591"):
            self.assertNotIn(leaked, s)
        self.assertIn("[телефон скрыт]", s)

    def test_keeps_medical_numbers(self):
        self.assertIn("7,35–7,45", mask_pii("pH крови 7,35–7,45"))


class ParseTest(unittest.TestCase):
    def test_plain_fenced_and_wrapped(self):
        raw = json.dumps(SAMPLE, ensure_ascii=False)
        for reply in (raw, f"```json\n{raw}\n```", f"Вот ответ: {raw} Готово."):
            self.assertEqual(parse_json(reply)["title"], SAMPLE["title"])

    def test_garbage(self):
        with self.assertRaises(ValueError):
            parse_json("не знаю")


class RenderTest(unittest.TestCase):
    def test_views(self):
        short = render.short(SAMPLE)
        self.assertIn("❌", short)
        self.assertIn("8/100", short)
        self.assertIn("УДД 1", render.details(SAMPLE))
        src = render.sources(SAMPLE)
        self.assertIn("pubmed.ncbi.nlm.nih.gov", src)
        self.assertNotIn("fake", src)  # неизвестный источник отброшен
        self.assertIn("6 шагов", render.self_check(SAMPLE))

    def test_escapes_html_and_survives_bad_data(self):
        bad = {"title": "<b>x</b>", "claims": [{"claim": "a<script>", "verdict": "??", "udd": "много"}], "overall": None}
        out = render.short(bad) + render.details(bad) + render.sources(bad)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;b&gt;", out)

    def test_length_limit(self):
        huge = dict(SAMPLE, summary="а" * 10000)
        self.assertLessEqual(len(render.short(huge)), 4000)


class CacheAndCheckerTest(unittest.TestCase):
    def test_same_content_hits_cache(self):
        self.assertEqual(make_key("Сода  ЛЕЧИТ", "", []), make_key("сода лечит", "", []))
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM([json.dumps(SAMPLE, ensure_ascii=False)])
            ch = Checker(cfg(tmp), llm=llm)
            key1, r1, c1 = asyncio.run(ch.check("Сода лечит рак, звони 89123456789", "", []))
            key2, r2, c2 = asyncio.run(ch.check("сода лечит рак, звони 89123456789", "", []))
            self.assertEqual((c1, c2), (False, True))
            self.assertEqual(llm.calls, 1)
            self.assertNotIn("89123456789", llm.last_prompt)
            self.assertEqual(ch.get(key1)["title"], SAMPLE["title"])
            ch.cache.db.close()

    def test_image_text_goes_into_prompt_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM([json.dumps(SAMPLE, ensure_ascii=False)])
            ch = Checker(cfg(tmp), llm=llm)
            asyncio.run(ch.check("", "", [("фото 1", b"fake-image-bytes")]))
            self.assertIn("ЧЕСНОК ЗАМЕНЯЕТ СТАТИНЫ", llm.last_prompt)
            self.assertNotIn("89001112233", llm.last_prompt)
            ch.cache.db.close()

    def test_retries_once_on_bad_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM(["извините", json.dumps(SAMPLE, ensure_ascii=False)])
            ch = Checker(cfg(tmp), llm=llm)
            _, r, _ = asyncio.run(ch.check("текст", "", []))
            self.assertEqual(llm.calls, 2)
            self.assertEqual(r["overall"]["score"], 8)
            ch.cache.db.close()

    def test_cache_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Cache(Path(tmp) / "c.db", days=0)
            c.put("k", {"a": 1})
            self.assertIsNone(c.get("k"))
            c.db.close()


class MediaTest(unittest.TestCase):
    def test_frames_and_audio_from_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=10",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest", "-y", str(video)], check=True)
            frames = asyncio.run(media.video_frames(video, 4))
            self.assertEqual(len(frames), 4)
            self.assertTrue(all(f[:2] == b"\xff\xd8" for f in frames))  # JPEG
            chunks = asyncio.run(media.audio_chunks(video))
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0][:4], b"OggS")


if __name__ == "__main__":
    unittest.main()
