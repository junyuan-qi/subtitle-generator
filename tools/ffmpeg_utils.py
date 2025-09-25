import os
import sys
import subprocess
import tempfile
import shutil

from .fs_utils import ensure_dirs


def extract_audio_ffmpeg(
    video_path: str, audio_path: str, overwrite: bool = False
) -> None:
    if os.path.exists(audio_path) and not overwrite:
        return
    ensure_dirs(os.path.dirname(audio_path))
    ext = os.path.splitext(audio_path)[1].lower()
    codec_args: list[str]
    if ext == ".mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", "128k"]
    elif ext in {".m4a", ".mp4"}:
        codec_args = ["-c:a", "aac", "-b:a", "128k"]
    elif ext in {".ogg", ".opus"}:
        codec_args = ["-c:a", "libopus", "-b:a", "96k"]
    else:
        codec_args = ["-c:a", "pcm_s16le"]
    cmd: list[str] = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-i",
        video_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        *codec_args,
        audio_path,
    ]
    try:
        _ = subprocess.run(
            cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        _handle_ffmpeg_error(e)
        raise


def _ffmpeg_filter_quote(value: str) -> str:
    """Quote a filter argument value for ffmpeg.

    Uses single quotes by default. If the value contains a single quote but not
    a double quote, prefer double quotes to avoid heavy escaping.
    """
    s = str(value)
    if "'" in s and '"' not in s:
        # Escape backslashes and double quotes inside double-quoted string
        s = s.replace("\\", r"\\").replace('"', r"\\\"")
        return '"' + s + '"'
    # Escape single quotes inside single-quoted string
    return "'" + s.replace("'", r"\'") + "'"


def _quote_style_value(value: str) -> str:
    """Return value, only quoting if truly necessary (commas/colons/quotes)."""
    # Spaces are safe in ASS style values; avoid adding nested quotes.
    needs_quote = any(ch in value for ch in ":;'\",")
    if not needs_quote:
        return value
    escaped = value.replace("\\", r"\\").replace("\"", r"\\\"")
    return f'"{escaped}"'


def _build_subtitle_style(
    font: str | None, font_size: int | None, margin_v: int | None
) -> str | None:
    """Build the force_style parameter for subtitle rendering."""
    style_parts: list[str] = []
    if font:
        style_parts.append(f"Fontname={_quote_style_value(str(font))}")
    if font_size:
        style_parts.append(f"Fontsize={int(font_size)}")
    if margin_v:
        style_parts.append(f"MarginV={int(margin_v)}")
    return ",".join(style_parts) if style_parts else None


def _build_subtitle_filter(srt_path: str, fonts_dir: str | None, force_style: str | None) -> str:
    """Build the subtitle filter string for ffmpeg."""
    filt = f"subtitles={_ffmpeg_filter_quote(srt_path)}:charenc=UTF-8"
    if fonts_dir:
        filt += f":fontsdir={_ffmpeg_filter_quote(fonts_dir)}"
    if force_style:
        filt += f":force_style={_ffmpeg_filter_quote(force_style)}"
    return filt


def _build_ffmpeg_command(
    video_path: str, subtitle_filter: str, out_path: str, show_progress: bool
) -> list[str]:
    """Build the complete ffmpeg command."""
    out_ext = os.path.splitext(out_path)[1].lower()
    cmd = ["ffmpeg", "-y"]
    if show_progress:
        cmd += ["-stats"]
    cmd += ["-i", video_path, "-vf", subtitle_filter]
    if out_ext == ".webm":
        cmd += ["-c:v", "libvpx-vp9", "-b:v", "2M", "-c:a", "libopus"]
    else:
        cmd += ["-c:v", "libx264", "-c:a", "copy"]
    cmd += [out_path]
    return cmd


def burn_subtitles_ffmpeg(
    video_path: str,
    srt_path: str,
    out_path: str,
    font: str | None = None,
    font_size: int | None = None,
    margin_v: int | None = None,
    fonts_dir: str | None = None,
    show_progress: bool = False,
) -> None:
    out_dir = os.path.dirname(out_path)
    if out_dir:
        ensure_dirs(out_dir)

    def _run(cmd: list[str]):
        return subprocess.run(
            cmd,
            check=True,
            stdout=None if show_progress else subprocess.PIPE,
            stderr=None if show_progress else subprocess.PIPE,
        )

    # If the SRT path has tricky quote characters, copy to a safe temp file name.
    tmp_srt_path: str | None = None
    try:
        if any(ch in srt_path for ch in "'\""):
            tmp_dir = tempfile.mkdtemp(prefix="subtitle_gen_srt_")
            tmp_srt_path = os.path.join(tmp_dir, "input.srt")
            try:
                shutil.copyfile(srt_path, tmp_srt_path)
                srt_for_filter = tmp_srt_path
            except Exception:
                # If copy fails, fall back to original path
                srt_for_filter = srt_path
        else:
            srt_for_filter = srt_path

        force_style = _build_subtitle_style(font, font_size, margin_v)
        subtitle_filter = _build_subtitle_filter(srt_for_filter, fonts_dir, force_style)
        cmd = _build_ffmpeg_command(video_path, subtitle_filter, out_path, show_progress)
        _ = _run(cmd)
    except subprocess.CalledProcessError as e:
        # If parsing failed due to style, retry without force_style as a fallback.
        stderr_text = ""
        try:
            if isinstance(e.stderr, (bytes, bytearray)):
                stderr_text = bytes(e.stderr).decode(errors="ignore")
        except Exception:
            pass

        parse_fail = any(
            token in stderr_text
            for token in (
                "Error parsing a filter description",
                "Error parsing filterchain",
                "No option name near",
            )
        )

        if parse_fail and ":force_style=" in subtitle_filter:
            # Build a simplified filter without force_style
            simplified = subtitle_filter.split(":force_style=", 1)[0]
            fallback_cmd = _build_ffmpeg_command(
                video_path, simplified, out_path, show_progress
            )
            try:
                _ = _run(fallback_cmd)
                return
            except subprocess.CalledProcessError as e2:
                _handle_ffmpeg_error(e2)
                raise
        else:
            _handle_ffmpeg_error(e)
            raise
    finally:
        if tmp_srt_path:
            try:
                os.remove(tmp_srt_path)
            except Exception:
                pass
            tmp_dir = os.path.dirname(tmp_srt_path)
            try:
                os.rmdir(tmp_dir)
            except Exception:
                pass


def detect_default_font() -> dict[str, str | None]:
    candidates: list[tuple[str, str]] = [
        (os.path.join("fonts", "Noto_Sans_SC"), "Noto Sans SC"),
        (os.path.join("fonts", "Noto Sans SC"), "Noto Sans SC"),
        ("fonts", "Noto Sans SC"),
    ]
    for dir_path, family in candidates:
        if os.path.isdir(dir_path):
            try:
                files: list[str] = [
                    f
                    for f in os.listdir(dir_path)
                    if f.lower().endswith((".ttf", ".otf"))
                ]
            except Exception:
                files = []
            if (
                files
                or dir_path.endswith("Noto_Sans_SC")
                or dir_path.endswith("Noto Sans SC")
            ):
                return {"fonts_dir": dir_path, "font_name": family}
    return {"fonts_dir": None, "font_name": None}


def ffprobe_duration_seconds(path: str) -> float | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out = proc.stdout.decode().strip()
        return float(out) if out else None
    except Exception:
        return None


def _handle_ffmpeg_error(error: subprocess.CalledProcessError) -> None:
    stderr_raw: object | None = getattr(error, "stderr", None)
    stderr_bytes: bytes | None = None
    if isinstance(stderr_raw, (bytes, bytearray)):
        stderr_bytes = bytes(stderr_raw)

    if stderr_bytes is not None:
        _ = sys.stderr.write(stderr_bytes.decode(errors="ignore"))
    else:
        _ = sys.stderr.write(str(error))
