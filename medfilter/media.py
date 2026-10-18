"""Видео: ключевые кадры и звуковая дорожка через ffmpeg. Фото: сжатие до JPEG."""
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


async def _run(*args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} завершился с ошибкой: {err.decode(errors='ignore')[-300:]}")
    return out


async def duration(path: Path) -> float:
    out = await _run(FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path))
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError):
        return 10.0


async def video_frames(path: Path, count: int = 4) -> list[bytes]:
    """Равномерно берёт count кадров, уменьшает до 1024 px по большей стороне."""
    d = await duration(path)
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            t = d * (i + 0.5) / count
            out = Path(tmp) / f"f{i}.jpg"
            await _run(FFMPEG, "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                       "-vf", "scale='min(1024,iw)':-2", "-q:v", "4", "-y", str(out))
            if out.exists():
                frames.append(out.read_bytes())
    return frames


async def audio_chunks(path: Path, chunk_sec: int = 25, max_chunks: int = 4) -> list[bytes]:
    """Звук в OGG Opus кусками по 25 с: синхронное распознавание SpeechKit принимает до 30 с."""
    d = await duration(path)
    chunks = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(min(max_chunks, int(d // chunk_sec) + 1)):
            out = Path(tmp) / f"a{i}.ogg"
            await _run(FFMPEG, "-v", "error", "-ss", str(i * chunk_sec), "-t", str(chunk_sec), "-i", str(path),
                       "-vn", "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", "-y", str(out))
            if out.exists() and out.stat().st_size > 1000:
                chunks.append(out.read_bytes())
    return chunks


async def to_jpeg(data: bytes, suffix: str = ".jpg") -> bytes:
    """Приводит любую картинку к JPEG до 1024 px."""
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / f"in{suffix}", Path(tmp) / "out.jpg"
        src.write_bytes(data)
        await _run(FFMPEG, "-v", "error", "-i", str(src), "-vf", "scale='min(1024,iw)':-2", "-q:v", "4", "-y", str(dst))
        return dst.read_bytes()
