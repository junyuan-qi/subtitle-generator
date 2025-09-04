import os
import sys
import subprocess
from typing import Dict

from .fs_utils import ensure_dirs


def extract_audio_ffmpeg(
    video_path: str, audio_path: str, overwrite: bool = False
) -> None:
    if os.path.exists(audio_path) and not overwrite:
        return
    ensure_dirs(os.path.dirname(audio_path))
    cmd = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-i",
        video_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        audio_path,
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(e.stderr.decode(errors="ignore"))
        raise


def _ffmpeg_filter_quote(value: str) -> str:
    return "'" + str(value).replace("'", r"\'") + "'"


def _build_subtitle_style(
    font: str | None, font_size: int | None, margin_v: int | None
) -> str | None:
    """Build the force_style parameter for subtitle rendering."""
    style_parts = []
    if font:
        style_parts.append(f"FontName={font}")
    if font_size:
        style_parts.append(f"FontSize={int(font_size)}")
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

    force_style = _build_subtitle_style(font, font_size, margin_v)
    subtitle_filter = _build_subtitle_filter(srt_path, fonts_dir, force_style)
    cmd = _build_ffmpeg_command(video_path, subtitle_filter, out_path, show_progress)

    try:
        if show_progress:
            subprocess.run(cmd, check=True)
        else:
            subprocess.run(
                cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
    except subprocess.CalledProcessError as e:
        sys.stderr.write(e.stderr.decode(errors="ignore"))
        raise


def detect_default_font() -> Dict[str, str | None]:
    candidates = [
        (os.path.join("fonts", "Noto_Sans_SC"), "Noto Sans SC"),
        (os.path.join("fonts", "Noto Sans SC"), "Noto Sans SC"),
        ("fonts", "Noto Sans SC"),
    ]
    for dir_path, family in candidates:
        if os.path.isdir(dir_path):
            try:
                files = [
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
