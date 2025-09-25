import argparse
import math
import os
import sys
import json
import subprocess
import tempfile
import time
from collections.abc import Sequence
from itertools import count
from dataclasses import dataclass
from typing import Protocol, TypedDict, cast

# Allow running as a script without requiring project root on sys.path
if __package__ is None or __package__ == "":  # pragma: no cover
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "tools"

# New modular helpers
from . import fs_utils
from .ffmpeg_utils import (
    extract_audio_ffmpeg as _extract_audio_ffmpeg_impl,
    burn_subtitles_ffmpeg as _burn_subtitles_ffmpeg_impl,
    detect_default_font as _detect_default_font_impl,
    ffprobe_duration_seconds as _ffprobe_duration_seconds_impl,
)

try:
    from dotenv import load_dotenv  # type: ignore
    _ = load_dotenv()  # auto-load .env from project root if present
except Exception:
    pass

# Lazy imports for SDKs to allow help/usage without deps installed

_COLOR_ENABLED = sys.stdout.isatty() and os.getenv("NO_COLOR") is None


def _style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR_ENABLED else text


def _hdr(text: str) -> str:
    return _style(text, "1;36")  # bold cyan


def _warn(text: str) -> str:
    return _style(text, "33")  # yellow


def _ok(text: str) -> str:
    return _style(text, "32")  # green


def _act(text: str) -> str:
    return _style(text, "35")  # magenta


def _label(text: str) -> str:
    return _style(text, "1")  # bold


def _err(text: str) -> str:
    return _style(text, "31")  # red


def _ordinal(n: int) -> str:
    """Return 1 -> 1st, 2 -> 2nd, 3 -> 3rd, etc."""
    if 10 <= (n % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _require_openai_client():
    try:
        from openai import OpenAI  # type: ignore
        return OpenAI
    except Exception:
        print("ERROR: openai SDK not installed. Add to requirements and install.")
        raise


def _is_retryable_openai_error(err: Exception) -> bool:
    should_retry = getattr(err, "should_retry", None)
    if isinstance(should_retry, bool):
        return should_retry

    try:
        from openai import (  # type: ignore
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
    except Exception:
        return False

    return isinstance(err, (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError))


def _require_gemini():
    try:
        import importlib
        genai = importlib.import_module("google.genai")  # provided by google-genai SDK
        return genai
    except Exception:
        print("ERROR: google-genai SDK not installed. Add to requirements and install.")
        raise


SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}

DEFAULT_GEMINI_TRANSLATION_BATCH_SIZE = 32

OPENAI_TRANSCRIBE_MAX_CONTENT_BYTES = 26_214_400  # 25 MiB limit from OpenAI API
OPENAI_TRANSCRIBE_SAFETY_FACTOR = 0.9  # keep chunks comfortably under the limit
OPENAI_FALLBACK_AUDIO_BYTES_PER_SEC = 16_000  # approx for 128 kbps mono MP3
OPENAI_MIN_CHUNK_DURATION = 1.0  # seconds; avoids zero-length slices when chunking


def _coerce_to_int(value: object) -> int | None:
    """Attempt to parse a loose JSON value into an int."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _coerce_to_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return float(stripped)
        except ValueError:
            return default
    return default


def _write_subprocess_error(error: subprocess.CalledProcessError) -> None:
    stderr_raw = getattr(error, "stderr", None)
    if isinstance(stderr_raw, (bytes, bytearray)):
        text = bytes(stderr_raw).decode(errors="ignore")
    elif stderr_raw is None:
        text = str(error)
    else:
        text = str(stderr_raw)
    _ = sys.stderr.write(text)


@dataclass
class Segment:
    start: float
    end: float
    text: str


class SrtBlock(TypedDict):
    index: str
    timing: str
    text: str


JSONDict = dict[str, object]


class _GeminiModels(Protocol):
    def generate_content(self, *, model: str, contents: Sequence[object]) -> object: ...


class GeminiClient(Protocol):
    models: _GeminiModels


def hhmmss_millis(seconds: float) -> str:
    if seconds < 0:
        seconds = 0

    # Compute total milliseconds first to avoid 1000ms rounding edge cases
    total_ms = int(round(seconds * 1000))
    total_seconds, millis = divmod(total_ms, 1000)

    hours = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    return f"{hours:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: Sequence[Segment], out_path: str) -> None:
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        start = hhmmss_millis(seg.start)
        end = hhmmss_millis(seg.end)
        text = seg.text.strip()
        if not text:
            continue
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")


def parse_srt(path: str) -> list[SrtBlock]:
    # Minimal SRT parser to extract blocks
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    blocks: list[SrtBlock] = []
    for block in content.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip() != ""]
        if len(lines) < 2:
            continue

        idx_line = lines[0].strip()
        timing_line = lines[1].strip()
        text_lines = lines[2:] if len(lines) > 2 else []

        blocks.append(
            SrtBlock(
                index=idx_line,
                timing=timing_line,
                text="\n".join(text_lines).strip(),
            )
        )

    return blocks


def assemble_srt(blocks: Sequence[SrtBlock]) -> str:
    out_lines: list[str] = []

    for i, b in enumerate(blocks, start=1):
        index = str(i)
        out_lines.append(index)
        out_lines.append(str(b["timing"]))
        text = str(b.get("text", "")).strip()
        out_lines.append(text)
        out_lines.append("")

    return "\n".join(out_lines).strip() + "\n"


def ensure_dirs(*dirs: str) -> None:  # legacy wrapper
    fs_utils.ensure_dirs(*dirs)


def find_videos(src_dir: str) -> list[str]:  # legacy wrapper
    return fs_utils.find_videos(src_dir)


def download_with_yt_dlp(
    urls: Sequence[str],
    dest_dir: str,
    fmt: str,
    output_tmpl: str,
    overwrite: bool,
    quiet: bool,
) -> None:
    """Download one or more URLs using yt-dlp into dest_dir.

    Requires the `yt-dlp` CLI on PATH.
    """
    # Check yt-dlp availability
    try:
        _ = subprocess.run(
            ["yt-dlp", "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception:
        print(
            _err(
                "yt-dlp not found. Install it (e.g., `pipx install yt-dlp` or `brew install yt-dlp`)."
            )
        )
        raise SystemExit(2)

    ensure_dirs(dest_dir)
    out_template = os.path.join(dest_dir, output_tmpl)

    for url in urls:
        print(_hdr("Downloading via yt-dlp"))
        print(f"{_label('URL:')} {url}")

        cmd = [
            "yt-dlp",
            "-f",
            fmt,
            "-o",
            out_template,
        ]

        if not overwrite:
            cmd.append("--no-overwrites")

        if not quiet:
            # Stream progress to console; --newline makes progress line-based
            cmd.append("--newline")
            cmd += [url]

            try:
                # Inherit stdout/stderr so progress displays live
                _ = subprocess.run(cmd, check=True)
                print(_ok("Download completed."))
            except subprocess.CalledProcessError:
                # Error already printed by yt-dlp; still raise to stop the pipeline
                raise

        else:
            # Quiet mode: capture output and only print summary
            cmd += ["--quiet", url]

            try:
                proc = subprocess.run(
                    cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                err_bytes = bytes(proc.stderr) if isinstance(proc.stderr, (bytes, bytearray)) else b""
                err = err_bytes.decode(errors="ignore").strip()
                print(_ok("Downloaded (or skipped if exists)."))
                if err and "ERROR" in err:
                    print(_warn(err))
            except subprocess.CalledProcessError as e:
                _write_subprocess_error(e)
                raise


def extract_audio_ffmpeg(
    video_path: str, audio_path: str, overwrite: bool = False
) -> None:  # legacy wrapper
    return _extract_audio_ffmpeg_impl(video_path, audio_path, overwrite=overwrite)


def burn_subtitles_ffmpeg(
    video_path: str,
    srt_path: str,
    out_path: str,
    font: str | None = None,
    font_size: int | None = None,
    margin_v: int | None = None,
    fonts_dir: str | None = None,
    show_progress: bool = False,
) -> None:  # legacy wrapper
    return _burn_subtitles_ffmpeg_impl(
        video_path=video_path,
        srt_path=srt_path,
        out_path=out_path,
        font=font,
        font_size=font_size,
        margin_v=margin_v,
        fonts_dir=fonts_dir,
        show_progress=show_progress,
    )


def _detect_default_font() -> dict[str, str | None]:  # legacy wrapper
    return _detect_default_font_impl()  # type: ignore[return-value]


def _ffprobe_duration_seconds(path: str) -> float | None:  # legacy wrapper
    return _ffprobe_duration_seconds_impl(path)


def _lang_display_name(code: str) -> str:
    """Return a human-friendly name for a language code (best effort)."""
    mapping = {
        "en": "English",
        "zh": "Chinese",
        "ja": "Japanese",
        "ko": "Korean",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "it": "Italian",
        "ru": "Russian",
        "pt": "Portuguese",
        "hi": "Hindi",
        "ar": "Arabic",
    }
    return mapping.get(code.lower(), code)


def _coerce_from_dict_methods(obj: object) -> JSONDict | None:
    for attr in ("model_dump", "to_dict", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                data = method()  # type: ignore[misc]
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items()}
            except Exception:
                continue
    return None


def _coerce_from_json_methods(obj: object) -> JSONDict | None:
    for attr in ("model_dump_json", "json"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                raw = method()  # type: ignore[misc]
                # json.loads expects str | bytes | bytearray; coerce otherwise
                if not isinstance(raw, (str, bytes, bytearray)):
                    raw = str(raw)
                data = json.loads(raw)
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items()}
            except Exception:
                continue
    return None


def _coerce_from_str(obj: object) -> JSONDict | None:
    try:
        data = json.loads(str(obj))
        return {str(k): v for k, v in data.items()} if isinstance(data, dict) else None
    except Exception:
        return None


def _coerce_openai_data(transcript: object) -> JSONDict:
    """Best-effort conversion of OpenAI transcript object to a plain dict."""
    if isinstance(transcript, dict):
        return dict(transcript)

    data = _coerce_from_dict_methods(transcript)
    if data is not None:
        return data

    data = _coerce_from_json_methods(transcript)
    if data is not None:
        return data

    data = _coerce_from_str(transcript)
    if data is not None:
        return data

    # Last resort: wrap text field if present
    return {"text": str(getattr(transcript, "text", ""))}


def _extract_segments(data: JSONDict) -> list[JSONDict]:
    maybe = data.get("segments")
    if not isinstance(maybe, list):
        return []

    segments: list[JSONDict] = []
    for item in maybe:
        if isinstance(item, dict):
            segments.append(dict(item))
    return segments


def _extract_text(data: JSONDict) -> str:
    val = data.get("text") if isinstance(data, dict) else None
    if isinstance(val, str):
        return val
    return str(val) if val is not None else ""


def _safe_audio_duration_seconds(path: str) -> float:
    duration = _ffprobe_duration_seconds(path)
    if duration is not None and duration > 0.0:
        return duration

    try:
        size = os.path.getsize(path)
    except OSError:
        return 0.0

    approx = size / float(OPENAI_FALLBACK_AUDIO_BYTES_PER_SEC)
    return approx if approx > 0.0 else 0.0


def _slice_audio_with_ffmpeg(
    source_path: str, out_path: str, start: float, duration: float | None
) -> None:
    ss_arg = f"{max(start, 0.0):.3f}"
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-ss",
        ss_arg,
        "-i",
        source_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
    ]
    if duration is not None and duration > 0.0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd.append(out_path)

    try:
        _ = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        _write_subprocess_error(error)
        raise


def _split_audio_for_transcription(
    audio_path: str, tmp_dir: str, max_bytes: int
) -> list[tuple[str, float]]:
    try:
        total_size = os.path.getsize(audio_path)
    except OSError:
        total_size = 0

    if total_size <= max_bytes:
        return [(audio_path, 0.0)]

    duration = _safe_audio_duration_seconds(audio_path)
    if duration <= 0.0:
        return [(audio_path, 0.0)]

    safe_max_bytes = max(1, int(max_bytes * OPENAI_TRANSCRIBE_SAFETY_FACTOR))
    chunk_count = max(2, math.ceil(total_size / safe_max_bytes))
    base_chunk_duration = max(duration / chunk_count, OPENAI_MIN_CHUNK_DURATION)

    chunks: list[tuple[str, float]] = []
    name_counter = count()

    def split_range(start: float, span: float) -> None:
        span = max(span, OPENAI_MIN_CHUNK_DURATION)
        if start >= duration:
            return

        remaining = max(duration - start, 0.0)
        if span > remaining:
            span = remaining
        if span <= 0.0:
            return

        chunk_path = os.path.join(tmp_dir, f"chunk_{next(name_counter):04d}.mp3")
        _slice_audio_with_ffmpeg(audio_path, chunk_path, start, span)

        try:
            chunk_size = os.path.getsize(chunk_path)
        except OSError:
            chunk_size = 0

        if chunk_size > max_bytes and span > OPENAI_MIN_CHUNK_DURATION * 1.5:
            os.remove(chunk_path)
            half = span / 2.0
            split_range(start, half)
            split_range(start + half, span - half)
            return

        if chunk_size > max_bytes:
            os.remove(chunk_path)
            raise RuntimeError(
                "Audio chunk still exceeds OpenAI size limit after splitting. "
                "Try re-encoding with a lower bitrate."
            )

        chunks.append((chunk_path, start))

    for index in range(chunk_count):
        start = min(index * base_chunk_duration, duration)
        if start >= duration:
            break
        span = base_chunk_duration if index < chunk_count - 1 else duration - start
        split_range(start, span)

    return chunks


def _openai_transcribe_chunk(
    client,
    audio_path: str,
    model: str,
    max_attempts: int,
) -> JSONDict:
    transcript: object | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with open(audio_path, "rb") as file_handle:
                if model == "whisper-1":
                    transcript = client.audio.transcriptions.create(
                        model=model,
                        file=file_handle,
                        response_format="verbose_json",
                        timestamp_granularities=["segment"],
                    )
                else:
                    transcript = client.audio.transcriptions.create(
                        model=model,
                        file=file_handle,
                        response_format="text",
                    )
            break
        except Exception as err:
            if attempt >= max_attempts or not _is_retryable_openai_error(err):
                raise

            delay = min(30.0, 2.0 * (2 ** (attempt - 1)))
            detail = str(err).strip() or err.__class__.__name__
            print(_warn(
                f"OpenAI request failed ({detail}). Retrying in {delay:.1f}s... [{attempt}/{max_attempts}]"
            ))
            time.sleep(delay)

    if transcript is None:
        raise RuntimeError("OpenAI transcription did not return a response")

    return _coerce_openai_data(transcript)


def _segments_from_transcript_dict(
    data: JSONDict, model: str, audio_path: str
) -> list[Segment]:
    segments_data = _extract_segments(data)

    if model != "whisper-1":
        text = _extract_text(data)
        dur = _safe_audio_duration_seconds(audio_path)
        return [Segment(start=0.0, end=dur, text=text or "")]

    if not segments_data:
        dur = _safe_audio_duration_seconds(audio_path)
        return [Segment(start=0.0, end=dur, text=_extract_text(data))]

    segments: list[Segment] = []
    for s in segments_data:
        start = _coerce_to_float(s.get("start"), 0.0)
        end = _coerce_to_float(s.get("end"), start)
        text = str(s.get("text", ""))
        segments.append(Segment(start=start, end=end, text=text))

    return segments


def transcribe_openai_verbose_json(
    audio_path: str, model: str = "whisper-1"
) -> list[Segment]:
    OpenAIClient = _require_openai_client()
    client = OpenAIClient()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment")

    raw_max_attempts = os.getenv("OPENAI_TRANSCRIBE_MAX_RETRIES", os.getenv("OPENAI_MAX_RETRIES", "3"))
    try:
        max_attempts = max(1, int(raw_max_attempts))
    except ValueError:
        max_attempts = 3

    try:
        total_size = os.path.getsize(audio_path)
    except OSError:
        total_size = 0

    max_bytes = OPENAI_TRANSCRIBE_MAX_CONTENT_BYTES

    if total_size <= max_bytes:
        data = _openai_transcribe_chunk(client, audio_path, model, max_attempts)
        return _segments_from_transcript_dict(data, model, audio_path)

    with tempfile.TemporaryDirectory(prefix="subtitle_gen_audio_split_") as tmp_dir:
        chunks = _split_audio_for_transcription(audio_path, tmp_dir, max_bytes)

        if len(chunks) == 1 and chunks[0][0] == audio_path:
            data = _openai_transcribe_chunk(client, audio_path, model, max_attempts)
            return _segments_from_transcript_dict(data, model, audio_path)

        print(
            _warn(
                f"Audio exceeds OpenAI size limit ({total_size} bytes). Splitting into {len(chunks)} chunk(s)."
            )
        )

        combined_segments: list[Segment] = []
        for idx, (chunk_path, offset) in enumerate(chunks, start=1):
            chunk_data = _openai_transcribe_chunk(client, chunk_path, model, max_attempts)
            chunk_segments = _segments_from_transcript_dict(chunk_data, model, chunk_path)

            if len(chunks) > 1:
                chunk_name = os.path.basename(chunk_path)
                print(_act(f"Transcribed chunk {idx}/{len(chunks)} ({chunk_name})"))

            for segment in chunk_segments:
                combined_segments.append(
                    Segment(
                        start=segment.start + offset,
                        end=segment.end + offset,
                        text=segment.text,
                    )
                )

    return combined_segments


def _normalize_gemini_model_name(name: str) -> str:
    alias_map = {
        # Common aliases or older naming
        "gemini-flash-2.5": "gemini-2.5-flash",
        "gemini-flash": "gemini-2.5-flash",
        "gemini-pro": "gemini-2.5-pro",
    }
    return alias_map.get(name, name)


def translate_texts_gemini(
    texts: Sequence[str],
    target_lang: str,
    model_name: str,
    batch_size: int = DEFAULT_GEMINI_TRANSLATION_BATCH_SIZE,
) -> list[str]:
    if not texts:
        return []

    genai = _require_gemini()

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY or GOOGLE_API_KEY in environment")

    client = cast(GeminiClient, genai.Client(api_key=api_key))
    model_name = _normalize_gemini_model_name(model_name)

    system_instructions = (
        "You are a professional subtitle translator. Translate each input string into "
        f"{target_lang} while preserving meaning, brevity, and readability.\n"
        "Rules:\n"
        "- Use the provided id for each subtitle.\n"
        "- Return ONLY JSON, no commentary.\n"
        "- Output format: array of objects with keys 'id' and 'translation', or a JSON object mapping id to translation.\n"
        "- Preserve id values exactly; do not renumber, add, or remove ids.\n"
        "- Do not include timestamps or numbers unless in the original text.\n"
        "- Add spaces between Chinese and Roman characters.\n"
    )

    results: list[str] = list(texts)
    batch_size = max(1, batch_size)

    indexed_texts = list(enumerate(texts))

    for start_idx in range(0, len(indexed_texts), batch_size):
        batch = indexed_texts[start_idx : start_idx + batch_size]
        translated_batch = _translate_text_batch_gemini(
            client=client,
            model_name=model_name,
            batch=batch,
            target_lang=target_lang,
            system_instructions=system_instructions,
        )

        for idx, text in translated_batch.items():
            if 0 <= idx < len(results):
                results[idx] = text

    return results


def _translate_text_batch_gemini(
    client: GeminiClient,
    model_name: str,
    batch: Sequence[tuple[int, str]],
    target_lang: str,
    system_instructions: str,
) -> dict[int, str]:
    payload = {
        "task": "translate_subtitles",
        "target_language": target_lang,
        "items": [{"id": idx, "text": text} for idx, text in batch],
    }

    try:
        resp = client.models.generate_content(
            model=model_name,
            contents=[
                system_instructions,
                "\nInput JSON:\n",
                json.dumps(payload, ensure_ascii=False),
                "\nRespond with only JSON in the requested format.\n",
            ],
        )
    except Exception as e:
        print(f"[tx] Gemini error: {e}")
        return {idx: text for idx, text in batch}

    out_text = None
    for attr in ("text", "output_text"):
        if hasattr(resp, attr):
            try:
                out_text = getattr(resp, attr)
                break
            except Exception:
                pass

    if out_text is None:
        out_text = str(resp)

    arr_text = out_text.strip()
    start = arr_text.find("[")
    end = arr_text.rfind("]")
    if start != -1 and end != -1 and end > start:
        arr_text = arr_text[start : end + 1]

    translations: dict[int, str] = {}

    try:
        data = json.loads(arr_text)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                idx = _coerce_to_int(item.get("id"))
                if idx is None:
                    continue
                value = item.get("translation")
                if value is None:
                    if "text" in item:
                        value = item["text"]
                    elif "value" in item:
                        value = item["value"]
                if value is not None:
                    translations[idx] = str(value)
        elif isinstance(data, dict):
            for key, value in data.items():
                idx = _coerce_to_int(key)
                if idx is None:
                    continue
                translations[idx] = str(value)
    except Exception:
        pass

    if translations:
        return translations

    return {idx: text for idx, text in batch}


def translate_srt_with_gemini(
    src_srt: str, out_srt: str, target_lang: str, model_name: str
) -> None:
    blocks = parse_srt(src_srt)
    if not blocks:
        raise RuntimeError(f"No SRT blocks found in {src_srt}")

    texts = [block["text"] for block in blocks]
    translated = translate_texts_gemini(
        texts, target_lang=target_lang, model_name=model_name
    )

    for i, t in enumerate(translated):
        blocks[i]["text"] = t

    srt = assemble_srt(blocks)
    os.makedirs(os.path.dirname(out_srt), exist_ok=True)
    with open(out_srt, "w", encoding="utf-8") as f:
        f.write(srt)


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Batch generate subtitles and translations from videos"
    )
    parser.add_argument("--src", default="videos", help="Source directory with videos")
    parser.add_argument("--audio", default="audio", help="Output directory for audio")
    parser.add_argument("--subs", default="subs", help="Output directory for SRT subtitles")
    parser.add_argument("--subs-lang", default="subs_zh", help="Output directory for translated SRT")
    parser.add_argument("--lang", default="zh", help="Target language for translation (default: zh)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files")

    # ASR options
    parser.add_argument("--asr-provider", default="openai", choices=["openai"], help="ASR provider")
    parser.add_argument("--asr-model", default="whisper-1",
                       help="OpenAI ASR model: whisper-1 (timestamps) or gpt-4o-transcribe/gpt-4o-mini-transcribe (text only)")

    # Translation options
    parser.add_argument("--tx-provider", default="gemini", choices=["gemini"], help="Translation provider")
    parser.add_argument("--tx-model", default="gemini-2.5-flash", help="Gemini model for translation")

    # Burned-in subtitles options
    parser.add_argument("--burn-in", action="store_true", help="Burn subtitles back into the video")
    parser.add_argument("--burn-use", default="translated", choices=["translated", "original"],
                       help="Which SRT to burn: translated or original")
    parser.add_argument("--burn-out", default="burned", help="Output directory for burned videos")
    parser.add_argument("--burn-font", default=None, help="Font name to use when burning (optional)")
    parser.add_argument("--burn-font-size", type=int, default=28, help="Font size for burned subtitles")
    parser.add_argument("--burn-margin-v", type=int, default=40, help="Vertical margin (bottom) for subtitles")
    parser.add_argument("--burn-fonts-dir", default=None, help="Directory with .ttf/.otf fonts to load (optional)")
    parser.add_argument("--burn-format", default="mp4", choices=["mp4", "webm"],
                       help="Container for burned output (default: mp4)")
    parser.add_argument("--burn-progress", action="store_true", help="Show ffmpeg progress while burning subtitles")

    # yt-dlp download options
    parser.add_argument("--yt", dest="yt_urls", action="append", default=None,
                       help="URL to download with yt-dlp before processing (repeat to add multiple)")
    parser.add_argument("--yt-format", dest="yt_format", default="bv*+ba/best",
                       help="yt-dlp format selection (default: bv*+ba/best)")
    parser.add_argument("--yt-output-tmpl", dest="yt_output_tmpl", default="%(title).200B.%(ext)s",
                       help="yt-dlp filename template (e.g., %%(title)s.%%(ext)s)")
    parser.add_argument("--yt-quiet", dest="yt_quiet", action="store_true",
                       help="Suppress yt-dlp progress output")
    return parser


def _validate_dependencies() -> bool:
    """Validate required dependencies are available."""
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        print("ERROR: ffmpeg not found. Please install ffmpeg and ensure it's on PATH.")
        return False


def _print_processing_header(args, videos):
    """Print the processing header information."""
    print(_hdr("Kicking Off"))
    print(f"{_label('Source:')} {args.src}")
    print(f"{_label('Videos to process:')} {len(videos)}")
    print(f"{_label('Burn-in:')} {'enabled' if args.burn_in else 'disabled'}")
    if args.burn_in:
        print(f"{_label('Output format:')} {args.burn_format}")

    print(_label("Files:"))
    for i, p in enumerate(videos, start=1):
        print(f"  {i}. {os.path.basename(p)}")
    print("")


def _generate_file_paths(video_path: str, args) -> dict[str, str]:
    """Generate all file paths for a video."""
    base = os.path.splitext(os.path.basename(video_path))[0]
    safe_base = base
    for ch in ["/", "\\", ":", "*", "?", '"', "<", ">", "|"]:
        safe_base = safe_base.replace(ch, "_")

    burned_ext = ".mp4" if args.burn_format == "mp4" else ".webm"
    lang_suffix = args.lang if args.burn_use == "translated" else "orig"

    return {
        "audio_path": os.path.join(args.audio, f"{safe_base}.mp3"),
        "srt_path": os.path.join(args.subs, f"{safe_base}.srt"),
        "translated_srt_path": os.path.join(args.subs_lang, f"{safe_base}.{args.lang}.srt"),
        "burned_out_path": os.path.join(args.burn_out, f"{safe_base}.{lang_suffix}.burned{burned_ext}")
    }


def _process_audio_step(video_path: str, audio_path: str, overwrite: bool):
    """Process the audio extraction step."""
    print(_hdr("Processing Audio"))
    if os.path.exists(audio_path) and not overwrite:
        print(f"{_warn('Skip exists:')} {audio_path}\n")
    else:
        print(f"{_act('Writing:')} {audio_path}")
        extract_audio_ffmpeg(video_path, audio_path, overwrite=overwrite)
        print(f"{_ok('Wrote:')} {audio_path}\n")


def _process_transcription_step(audio_path: str, srt_path: str, asr_model: str, overwrite: bool):
    """Process the transcription step."""
    print(_hdr("Transcribing"))
    if not os.path.exists(srt_path) or overwrite:
        segments = transcribe_openai_verbose_json(audio_path, model=asr_model)
        write_srt(segments, srt_path)
        print(f"{_ok('Wrote:')} {srt_path}\n")
    else:
        print(f"{_warn('Skip exists:')} {srt_path}\n")


def _process_translation_step(srt_path: str, translated_srt_path: str, target_lang: str, tx_model: str, overwrite: bool):
    """Process the translation step."""
    lang_name = _lang_display_name(target_lang)
    print(_hdr(f"Translating to {lang_name}"))
    if not os.path.exists(translated_srt_path) or overwrite:
        translate_srt_with_gemini(srt_path, translated_srt_path, target_lang=target_lang, model_name=tx_model)
        print(f"{_ok('Wrote:')} {translated_srt_path}\n")
    else:
        print(f"{_warn('Skip exists:')} {translated_srt_path}\n")


def _process_burn_step(video_path: str, file_paths: dict[str, str], args):
    """Process the subtitle burning step."""
    detected = _detect_default_font()
    if not args.burn_font and detected.get("font_name"):
        args.burn_font = detected["font_name"]
    if not args.burn_fonts_dir and detected.get("fonts_dir"):
        args.burn_fonts_dir = detected["fonts_dir"]

    srt_to_use = file_paths["translated_srt_path"] if args.burn_use == "translated" else file_paths["srt_path"]

    print(_hdr("Burning Subtitles"))
    if os.path.exists(file_paths["burned_out_path"]) and not args.overwrite:
        print(f"{_warn('Skip exists:')} {file_paths['burned_out_path']}\n")
    elif not os.path.exists(srt_to_use):
        print(f"{_err('SRT not found:')} {srt_to_use}\n")
    else:
        if args.burn_font:
            print(f"{_label('Font:')} {args.burn_font}")
        if args.burn_fonts_dir:
            print(f"{_label('Fonts dir:')} {args.burn_fonts_dir}")
        burn_subtitles_ffmpeg(
            video_path=video_path, srt_path=srt_to_use, out_path=file_paths["burned_out_path"],
            font=args.burn_font, font_size=args.burn_font_size, margin_v=args.burn_margin_v,
            fonts_dir=args.burn_fonts_dir, show_progress=args.burn_progress
        )
        print(f"{_ok('Wrote:')} {file_paths['burned_out_path']}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Main entry point for the subtitle generation tool."""
    parser = _create_argument_parser()
    parsed_args = parser.parse_args(list(argv) if argv is not None else None)
    args = parsed_args

    if not _validate_dependencies():
        return 2

    ensure_dirs(args.audio, args.subs, args.subs_lang)

    if args.yt_urls:
        download_with_yt_dlp(
            args.yt_urls, dest_dir=args.src, fmt=args.yt_format,
            output_tmpl=args.yt_output_tmpl, overwrite=args.overwrite, quiet=args.yt_quiet
        )

    videos = find_videos(args.src)
    if not videos:
        print(f"No videos found in {args.src}")
        return 0

    _print_processing_header(args, videos)

    for i, video_path in enumerate(videos, start=1):
        file_paths = _generate_file_paths(video_path, args)

        print(_hdr(f"Start processing the {_ordinal(i)} file"))
        print(_label(os.path.basename(video_path)))
        print("")

        _process_audio_step(video_path, file_paths["audio_path"], args.overwrite)
        _process_transcription_step(file_paths["audio_path"], file_paths["srt_path"], args.asr_model, args.overwrite)
        _process_translation_step(file_paths["srt_path"], file_paths["translated_srt_path"], args.lang, args.tx_model, args.overwrite)

        if args.burn_in:
            _process_burn_step(video_path, file_paths, args)

    print(_hdr("All done."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
