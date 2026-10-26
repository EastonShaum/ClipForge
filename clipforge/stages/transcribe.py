"""Transcribe stage: audio → word-level timestamps via a pluggable provider.

Providers return {"words": [{"w", "s", "e"}...], "language", "provider", "model", "audio_s",
"usd"}. Times are integer milliseconds.
"""

import json
import subprocess
import time
from pathlib import Path

import httpx

from clipforge.config import Settings

CHUNK_S = 600  # 10-minute chunks keep each upload far below API size limits
OVERLAP_S = 2.0


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def extract_audio(media: Path, out: Path, start: float = 0.0, length: float | None = None) -> Path:
    """16 kHz mono 32 kbps Opus: ~14 MB per hour, plenty for speech recognition."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(media)]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k", str(out)]
    subprocess.run(cmd, check=True)
    return out


def _post_with_retry(client: httpx.Client, url: str, **kw) -> httpx.Response:
    for attempt in range(5):
        r = client.post(url, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("retry-after", 2 ** attempt * 5))
            time.sleep(min(wait, 120))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def _openai_compatible(s: Settings, audio: Path, base: str, key: str, model: str) -> dict:
    with httpx.Client(timeout=300) as client, audio.open("rb") as fh:
        r = _post_with_retry(
            client,
            f"{base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (audio.name, fh, "audio/ogg")},
            data={"model": model, "response_format": "verbose_json",
                  "timestamp_granularities[]": "word"},
        )
    return r.json()


def transcribe_chunked(s: Settings, media: Path, work: Path, call) -> tuple[list[dict], str]:
    """Split long audio into overlapping chunks, transcribe each, stitch word lists."""
    total = probe_duration(media)
    words: list[dict] = []
    language = ""
    start = 0.0
    idx = 0
    while start < total:
        length = min(CHUNK_S + OVERLAP_S, total - start)
        chunk = extract_audio(media, work / f"chunk{idx:03d}.ogg", start, length)
        data = call(chunk)
        language = language or data.get("language", "")
        cutoff_ms = int(start * 1000) + (int(CHUNK_S * 1000) if start + length < total else 10**12)
        floor_ms = words[-1]["e"] if words else -1
        for w in data.get("words", []):
            s_ms = int(start * 1000 + float(w["start"]) * 1000)
            e_ms = int(start * 1000 + float(w["end"]) * 1000)
            # Overlap region: keep words from the earlier chunk, drop duplicates.
            if s_ms < floor_ms or s_ms >= cutoff_ms:
                continue
            words.append({"w": w["word"].strip(), "s": s_ms, "e": e_ms})
        start += CHUNK_S
        idx += 1
    return words, language


def transcribe(s: Settings, media_path: str, work: Path) -> dict:
    media = Path(media_path)
    work.mkdir(parents=True, exist_ok=True)
    provider = s.transcribe_provider
    if provider == "fake":
        return fake_transcript(probe_duration(media))
    if provider == "groq":
        if not s.groq_api_key:
            raise MissingKey("CF_GROQ_API_KEY is not set")
        base, key, model = "https://api.groq.com/openai/v1", s.groq_api_key, s.transcribe_model
    elif provider == "openai":
        if not s.openai_api_key:
            raise MissingKey("CF_OPENAI_API_KEY is not set")
        base, key, model = "https://api.openai.com/v1", s.openai_api_key, "whisper-1"
    elif provider == "local":
        return transcribe_local(s, media, work)
    else:
        raise ValueError(f"unknown transcribe provider {provider}")
    words, lang = transcribe_chunked(
        s, media, work, lambda chunk: _openai_compatible(s, chunk, base, key, model)
    )
    from clipforge.costs import stt_cost

    audio_s = probe_duration(media)
    return {"words": words, "language": lang, "provider": provider, "model": model,
            "audio_s": audio_s, "usd": stt_cost(provider, model, audio_s)}


def transcribe_local(s: Settings, media: Path, work: Path) -> dict:
    """faster-whisper on the laptop worker (optional dependency)."""
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    audio = extract_audio(media, work / "full.ogg")
    model = WhisperModel(s.transcribe_model if "/" in s.transcribe_model else "small",
                         device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(audio), word_timestamps=True, vad_filter=True)
    words = [{"w": w.word.strip(), "s": int(w.start * 1000), "e": int(w.end * 1000)}
             for seg in segments for w in (seg.words or [])]
    return {"words": words, "language": info.language, "provider": "local",
            "model": "faster-whisper", "audio_s": info.duration, "usd": 0.0}


class MissingKey(RuntimeError):
    pass


FAKE_SENTENCES = [
    "Nobody tells you this but the first year almost broke me completely.",
    "I quit my job with three months of savings and it was the best decision I ever made.",
    "Here is the thing about discipline that most people get completely wrong.",
    "Wait wait wait, did he really just clutch that with one health point left?",
    "The craziest part is that nobody in the chat believed it until the replay.",
]


def fake_transcript(duration_s: float) -> dict:
    """Deterministic placeholder transcript so the pipeline runs end to end without keys."""
    words = []
    t = 500
    i = 0
    while t < duration_s * 1000 - 600:
        for w in FAKE_SENTENCES[i % len(FAKE_SENTENCES)].split():
            if t >= duration_s * 1000 - 600:
                break
            words.append({"w": w, "s": t, "e": t + 280})
            t += 330
        t += 600
        i += 1
    return {"words": words, "language": "en", "provider": "fake", "model": "fake",
            "audio_s": duration_s, "usd": 0.0}
