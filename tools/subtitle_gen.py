import argparse
import os
import sys
import json
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()  # auto-load .env from project root if present
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


def _require_gemini():
    try:
        import importlib
        genai = importlib.import_module("google.genai")  # provided by google-genai SDK
        return genai
    except Exception:
        print("ERROR: google-genai SDK not installed. Add to requirements and install.")
        raise


SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


@dataclass
class Segment:
    start: float
    end: float
    text: str


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


def write_srt(segments: List[Segment], out_path: str) -> None:
    lines: List[str] = []
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


def parse_srt(path: str) -> List[Dict[str, Any]]:
    # Minimal SRT parser to extract blocks
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    blocks = []
    for block in content.strip().split("\n\n"):
        lines = [l for l in block.splitlines() if l.strip() != ""]
        if len(lines) < 2:
            continue
        idx_line = lines[0].strip()
        timing_line = lines[1].strip()
        text_lines = lines[2:] if len(lines) > 2 else []
        blocks.append({
            "index": idx_line,
            "timing": timing_line,
            "text": "\n".join(text_lines).strip(),
        })
    return blocks


def assemble_srt(blocks: List[Dict[str, Any]]) -> str:
    out_lines: List[str] = []
    for i, b in enumerate(blocks, start=1):
        index = str(i)
        out_lines.append(index)
        out_lines.append(str(b["timing"]))
        text = str(b.get("text", "")).strip()
        out_lines.append(text)
        out_lines.append("")
    return "\n".join(out_lines).strip() + "\n"


def ensure_dirs(*dirs: str) -> None:
    for d in dirs:
        os.makedirs(d, exist_ok=True)


def find_videos(src_dir: str) -> List[str]:
    paths: List[str] = []
    if not os.path.isdir(src_dir):
        return paths
    for entry in sorted(os.listdir(src_dir)):
        p = os.path.join(src_dir, entry)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext in SUPPORTED_VIDEO_EXTS:
            paths.append(p)
    return paths


def extract_audio_ffmpeg(video_path: str, audio_path: str, overwrite: bool = False) -> None:
    if os.path.exists(audio_path) and not overwrite:
        # Caller prints section + skip; keep function quiet on skip.
        return
    ensure_dirs(os.path.dirname(audio_path))
    cmd = [
        "ffmpeg", "-y" if overwrite else "-n",
        "-i", video_path,
        "-ac", "1",  # mono
        "-ar", "16000",  # 16 kHz
        "-vn",
        audio_path,
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(e.stderr.decode(errors="ignore"))
        raise


def _ffmpeg_filter_quote(value: str) -> str:
    """Quote a value for use inside an ffmpeg filter option.
    Uses single quotes and escapes internal single quotes.
    """
    return "'" + str(value).replace("'", r"\'") + "'"


def burn_subtitles_ffmpeg(
    video_path: str,
    srt_path: str,
    out_path: str,
    font: Optional[str] = None,
    font_size: Optional[int] = None,
    margin_v: Optional[int] = None,
    fonts_dir: Optional[str] = None,
) -> None:
    """Burn subtitles into video using ffmpeg subtitles filter.
    Requires ffmpeg with libass support.
    """
    out_dir = os.path.dirname(out_path)
    if out_dir:
        ensure_dirs(out_dir)

    # Build subtitles filter with optional force_style for font and size
    style_parts = []
    if font:
        style_parts.append(f"FontName={font}")
    if font_size:
        style_parts.append(f"FontSize={int(font_size)}")
    if margin_v:
        style_parts.append(f"MarginV={int(margin_v)}")
    force_style = None
    if style_parts:
        force_style = ",".join(style_parts)

    # Use UTF-8 char encoding for SRT
    # Construct filter argument
    filt = f"subtitles={_ffmpeg_filter_quote(srt_path)}:charenc=UTF-8"
    if fonts_dir:
        filt += f":fontsdir={_ffmpeg_filter_quote(fonts_dir)}"
    if force_style:
        filt += f":force_style={_ffmpeg_filter_quote(force_style)}"

    # Choose codecs by output container
    out_ext = os.path.splitext(out_path)[1].lower()
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vf", filt]
    if out_ext == ".webm":
        # WebM requires VP8/VP9 video and Vorbis/Opus audio. Use VP9 + Opus.
        cmd += ["-c:v", "libvpx-vp9", "-b:v", "2M", "-c:a", "libopus"]
    else:
        # Default to H.264 for mp4/mov, and copy audio to preserve quality.
        cmd += ["-c:v", "libx264", "-c:a", "copy"]
    cmd += [out_path]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(e.stderr.decode(errors="ignore"))
        raise


def _detect_default_font() -> Dict[str, Optional[str]]:
    """Detect a bundled CJK font directory and family name.
    Returns a dict with keys: fonts_dir, font_name (either may be None).
    Preference order:
    - fonts/Noto_Sans_SC (Noto Sans SC family)
    - fonts (if it contains any NotoSansSC/Noto Sans SC TTF/OTF)
    """
    candidates = [
        (os.path.join("fonts", "Noto_Sans_SC"), "Noto Sans SC"),
        (os.path.join("fonts", "Noto Sans SC"), "Noto Sans SC"),
        ("fonts", "Noto Sans SC"),
    ]
    for dir_path, family in candidates:
        if os.path.isdir(dir_path):
            # Check for any .ttf/.otf present (optional for first two)
            try:
                files = [f for f in os.listdir(dir_path) if f.lower().endswith((".ttf", ".otf"))]
            except Exception:
                files = []
            if files or dir_path.endswith("Noto_Sans_SC") or dir_path.endswith("Noto Sans SC"):
                return {"fonts_dir": dir_path, "font_name": family}
    return {"fonts_dir": None, "font_name": None}


def _ffprobe_duration_seconds(path: str) -> Optional[float]:
    try:
        proc = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = proc.stdout.decode().strip()
        return float(out) if out else None
    except Exception:
        return None


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


def _coerce_from_dict_methods(obj: Any) -> Optional[Dict[str, Any]]:
    for attr in ("model_dump", "to_dict", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                data = method()  # type: ignore[misc]
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return None


def _coerce_from_json_methods(obj: Any) -> Optional[Dict[str, Any]]:
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
                    return data
            except Exception:
                continue
    return None


def _coerce_from_str(obj: Any) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(str(obj))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _coerce_openai_data(transcript: Any) -> Dict[str, Any]:
    """Best-effort conversion of OpenAI transcript object to a plain dict."""
    if isinstance(transcript, dict):
        return transcript  # type: ignore[return-value]

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


def _extract_segments(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    maybe = data.get("segments")
    return maybe if isinstance(maybe, list) else []


def _extract_text(data: Dict[str, Any]) -> str:
    val = data.get("text") if isinstance(data, dict) else None
    if isinstance(val, str):
        return val
    return str(val) if val is not None else ""


def transcribe_openai_verbose_json(audio_path: str, model: str = "whisper-1") -> List[Segment]:
    OpenAIClient = _require_openai_client()
    client = OpenAIClient()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment")

    if model == "whisper-1":
        with open(audio_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model=model,
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
    else:
        # gpt-4o(-mini)-transcribe support only text/json; no segments
        with open(audio_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model=model,
                file=f,
                response_format="text",
            )

    # Convert SDK response to plain dict
    data: Dict[str, Any] = _coerce_openai_data(transcript)
    segments_data = _extract_segments(data)
    if model != "whisper-1":
        # Build a single segment spanning the audio duration
        text = _extract_text(data)
        dur = _ffprobe_duration_seconds(audio_path) or 0.0
        return [Segment(start=0.0, end=dur, text=text or "")]
    if not segments_data:
        # Fallback: no segments from whisper; single full segment with duration
        dur = _ffprobe_duration_seconds(audio_path) or 0.0
        return [Segment(start=0.0, end=dur, text=_extract_text(data))]

    segments: List[Segment] = []
    for s in segments_data:
        if isinstance(s, dict):
            start = float(s.get("start", 0.0))
            end = float(s.get("end", start))
            text = str(s.get("text", ""))
            segments.append(Segment(start=start, end=end, text=text))
    return segments


def _normalize_gemini_model_name(name: str) -> str:
    alias_map = {
        # Common aliases or older naming
        "gemini-flash-2.5": "gemini-2.5-flash",
        "gemini-flash": "gemini-2.5-flash",
        "gemini-pro": "gemini-2.5-pro",
    }
    return alias_map.get(name, name)


def translate_texts_gemini(texts: List[str], target_lang: str, model_name: str) -> List[str]:
    genai = _require_gemini()
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY or GOOGLE_API_KEY in environment")

    client = genai.Client(api_key=api_key)
    model_name = _normalize_gemini_model_name(model_name)

    # Ask for strict JSON array of translated strings to preserve order
    system_instructions = (
        "You are a professional subtitle translator. Translate each input string into "
        f"{target_lang} while preserving meaning, brevity, and readability.\n"
        "Rules:\n"
        "- Return ONLY a JSON array of strings, no commentary.\n"
        "- Keep order and number of items exactly the same as input.\n"
        "- Do not add or remove items.\n"
        "- Do not include timestamps or numbers unless in the original text.\n"
    )

    payload = {
        "task": "translate_subtitles",
        "target_language": target_lang,
        "items": texts,
    }

    # New SDK: call via client.models.generate_content
    try:
        resp = client.models.generate_content(
            model=model_name,
            contents=[
                system_instructions,
                "\nInput JSON:\n",
                json.dumps(payload, ensure_ascii=False),
                "\nRespond with only a JSON array of strings matching items length.\n",
            ],
        )
    except Exception as e:
        print(f"[tx] Gemini error: {e}")
        return texts

    # Extract text robustly
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
        arr_text = arr_text[start:end + 1]
    try:
        data = json.loads(arr_text)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return texts


def translate_srt_with_gemini(src_srt: str, out_srt: str, target_lang: str, model_name: str) -> None:
    blocks = parse_srt(src_srt)
    if not blocks:
        raise RuntimeError(f"No SRT blocks found in {src_srt}")
    texts = [b.get("text", "") for b in blocks]
    translated = translate_texts_gemini(texts, target_lang=target_lang, model_name=model_name)
    if len(translated) != len(blocks):
        print("[warn] Translation count mismatch; keeping original texts for safety")
        translated = texts
    for i, t in enumerate(translated):
        blocks[i]["text"] = t
    srt = assemble_srt(blocks)
    os.makedirs(os.path.dirname(out_srt), exist_ok=True)
    with open(out_srt, "w", encoding="utf-8") as f:
        f.write(srt)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Batch generate subtitles and translations from videos")
    parser.add_argument("--src", default="videos", help="Source directory with videos")
    parser.add_argument("--audio", default="audio", help="Output directory for audio")
    parser.add_argument("--subs", default="subs", help="Output directory for SRT subtitles")
    parser.add_argument("--subs-lang", default="subs_zh", help="Output directory for translated SRT")
    parser.add_argument("--lang", default="zh", help="Target language for translation (default: zh)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    # ASR
    parser.add_argument("--asr-provider", default="openai", choices=["openai"], help="ASR provider")
    parser.add_argument(
        "--asr-model",
        default="whisper-1",
        help="OpenAI ASR model: whisper-1 (timestamps) or gpt-4o-transcribe/gpt-4o-mini-transcribe (text only)",
    )
    # Translation
    parser.add_argument("--tx-provider", default="gemini", choices=["gemini"], help="Translation provider")
    parser.add_argument("--tx-model", default="gemini-2.5-flash", help="Gemini model for translation")
    # Burned-in subtitles
    parser.add_argument("--burn-in", action="store_true", help="Burn subtitles back into the video")
    parser.add_argument("--burn-use", default="translated", choices=["translated", "original"], help="Which SRT to burn: translated or original")
    parser.add_argument("--burn-out", default="burned", help="Output directory for burned videos")
    parser.add_argument("--burn-font", default=None, help="Font name to use when burning (optional)")
    parser.add_argument("--burn-font-size", type=int, default=28, help="Font size for burned subtitles")
    parser.add_argument("--burn-margin-v", type=int, default=40, help="Vertical margin (bottom) for subtitles")
    parser.add_argument("--burn-fonts-dir", default=None, help="Directory with .ttf/.otf fonts to load (optional)")
    parser.add_argument("--burn-format", default="mp4", choices=["mp4", "webm"], help="Container for burned output (default: mp4)")

    args = parser.parse_args(argv)

    # Validate ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception:
        print("ERROR: ffmpeg not found. Please install ffmpeg and ensure it's on PATH.")
        return 2

    ensure_dirs(args.audio, args.subs, args.subs_lang)

    videos = find_videos(args.src)
    if not videos:
        print(f"No videos found in {args.src}")
        return 0

    # Run header
    print(_hdr("Kicking Off"))
    print(f"{_label('Source:')} {args.src}")
    print(f"{_label('Videos to process:')} {len(videos)}")
    print(f"{_label('Burn-in:')} {'enabled' if args.burn_in else 'disabled'}")
    if args.burn_in:
        print(f"{_label('Output format:')} {args.burn_format}")
    # List filenames to be processed
    print(_label("Files:"))
    for i, p in enumerate(videos, start=1):
        print(f"  {i}. {os.path.basename(p)}")
    print("")

    for i, v in enumerate(videos, start=1):
        base = os.path.splitext(os.path.basename(v))[0]
        safe_base = base
        # Replace path-unfriendly characters for outputs
        for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
            safe_base = safe_base.replace(ch, '_')

        audio_path = os.path.join(args.audio, f"{safe_base}.wav")
        srt_path = os.path.join(args.subs, f"{safe_base}.srt")
        translated_srt_path = os.path.join(args.subs_lang, f"{safe_base}.{args.lang}.srt")
        burned_ext = ".mp4" if args.burn_format == "mp4" else ".webm"
        burned_out_path = os.path.join(
            args.burn_out,
            f"{safe_base}.{args.lang if args.burn_use=='translated' else 'orig'}.burned{burned_ext}"
        )

        print(_hdr(f"Start processing the {_ordinal(i)} file"))
        print(_label(os.path.basename(v)))
        print("")

        # 1) Extract audio
        print(_hdr("Processing Audio"))
        if os.path.exists(audio_path) and not args.overwrite:
            print(f"{_warn('Skip exists:')} {audio_path}\n")
        else:
            print(f"{_act('Writing:')} {audio_path}")
            extract_audio_ffmpeg(v, audio_path, overwrite=args.overwrite)
            print(f"{_ok('Wrote:')} {audio_path}\n")

        # 2) Transcribe to SRT
        print(_hdr("Transcribing"))
        if not os.path.exists(srt_path) or args.overwrite:
            segments = transcribe_openai_verbose_json(audio_path, model=args.asr_model)
            write_srt(segments, srt_path)
            print(f"{_ok('Wrote:')} {srt_path}\n")
        else:
            print(f"{_warn('Skip exists:')} {srt_path}\n")

        # 3) Translate SRT
        lang_name = _lang_display_name(args.lang)
        print(_hdr(f"Translating to {lang_name}"))
        if not os.path.exists(translated_srt_path) or args.overwrite:
            translate_srt_with_gemini(srt_path, translated_srt_path, target_lang=args.lang, model_name=args.tx_model)
            print(f"{_ok('Wrote:')} {translated_srt_path}\n")
        else:
            print(f"{_warn('Skip exists:')} {translated_srt_path}\n")

        # 4) Burn subtitles back into video if requested
        if args.burn_in:
            # Auto-default font if not provided and bundled fonts exist
            if not args.burn_font or not args.burn_fonts_dir:
                detected = _detect_default_font()
                if not args.burn_font and detected.get("font_name"):
                    args.burn_font = detected["font_name"]  # type: ignore
                if not args.burn_fonts_dir and detected.get("fonts_dir"):
                    args.burn_fonts_dir = detected["fonts_dir"]  # type: ignore
            srt_to_use = translated_srt_path if args.burn_use == "translated" else srt_path
            print(_hdr("Burning Subtitles"))
            # Skip if burned output already exists unless --overwrite is set
            if os.path.exists(burned_out_path) and not args.overwrite:
                print(f"{_warn('Skip exists:')} {burned_out_path}\n")
            elif not os.path.exists(srt_to_use):
                print(f"{_err('SRT not found:')} {srt_to_use}\n")
            else:
                if args.burn_font:
                    print(f"{_label('Font:')} {args.burn_font}")
                if args.burn_fonts_dir:
                    print(f"{_label('Fonts dir:')} {args.burn_fonts_dir}")
                burn_subtitles_ffmpeg(
                    video_path=v,
                    srt_path=srt_to_use,
                    out_path=burned_out_path,
                    font=args.burn_font,
                    font_size=args.burn_font_size,
                    margin_v=args.burn_margin_v,
                    fonts_dir=args.burn_fonts_dir,
                )
                print(f"{_ok('Wrote:')} {burned_out_path}\n")

    print(_hdr("All done."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
