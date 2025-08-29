# Subtitle Generator

Small CLI to batch-generate subtitles (SRT) from videos and translate them.

Features:
- Extracts audio with ffmpeg.
- Transcribes via OpenAI Speech-to-Text to SRT.
  - `whisper-1` (default) returns timestamped segments for accurate SRT.
  - `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` return plain text only; the script emits a single full-span SRT block.
- Translates SRT with Google Gemini via the new `google-genai` SDK (default `gemini-2.5-flash`).
- Skips existing outputs unless `--overwrite` is provided.

## Prerequisites

- Python 3.9+
- `ffmpeg` on PATH (`ffmpeg -version`)
- API keys as environment variables (auto-loaded from `.env`):
  - `OPENAI_API_KEY` for transcription
  - `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) for Gemini translation

## Setup (uv)

Using `uv` for environment and dependency management:

```bash
# Ensure you have uv installed: https://docs.astral.sh/uv/
uv sync   # creates .venv and installs deps from pyproject/uv.lock
```

Alternatively with pip:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Place videos under `videos/` (default). Supported inputs: mp4, mov, mkv, avi, m4v, webm. Then run:

```bash
# .env is auto-loaded (python-dotenv). Place keys in .env:
# OPENAI_API_KEY=sk-...
# GOOGLE_API_KEY=AIza...

# via console script (preferred) — accepts any Gemini model (e.g., `gemini-2.5-flash`, `gemini-1.5-flash`, etc.)
uv run subtitle-gen \
  --src videos \
  --audio audio \
  --subs subs \
  --subs-lang subs_zh \
  --lang zh \
  --asr-model whisper-1 \
  --tx-model gemini-2.5-flash

# or via main.py wrapper (enables burn-in by default)
uv run main.py --src videos --lang zh

Note: Running via `main.py` enables `--burn-in` automatically. Use the `subtitle-gen` CLI if you prefer not to burn-in by default, or pass the explicit CLI flags with `subtitle-gen`.
```

Outputs:
- `audio/<video>.wav`
- `subs/<video>.srt` (original language)
- `subs_zh/<video>.zh.srt` (translated)
 - `burned/<video>.<lang|orig>.burned.<mp4|webm>` (optional burned-in; default mp4)

### Notes

- You can set `--asr-model gpt-4o-mini-transcribe` if available in your account.
- For translation model, you can use any supported Gemini text model (e.g., `gemini-1.5-flash`).
- The translator batches lines and requests a strict JSON array to preserve order.

### Burn-in subtitles (optional)

Burn the SRT back into the video using ffmpeg (requires libass support in ffmpeg):

Multi-line (note the trailing backslashes):

```bash
uv run subtitle-gen \
  --src videos \
  --lang zh \
  --burn-in \
  --burn-use translated \
  --burn-out burned \
  --burn-font "PingFang SC" \
  --burn-font-size 28 \
  --burn-margin-v 40 \
  # optionally choose output container (default mp4)
  --burn-format webm
```

Single-line equivalent:

```bash
uv run subtitle-gen --src videos --lang zh --burn-in --burn-use translated --burn-out burned --burn-font "PingFang SC" --burn-font-size 28 --burn-margin-v 40 --burn-format mp4
```

Notes:
- `--burn-use translated` uses the translated SRT (e.g., Chinese). Use `original` to burn the source-language SRT.
- For CJK text, specify a font that supports Chinese via `--burn-font` (macOS: `PingFang SC`, `Songti SC`, `Hiragino Sans GB`; cross‑platform: `Noto Sans CJK SC`).
- If the system can’t find the font, bundle it locally and point ffmpeg to it:
  - Put `.otf/.ttf` files under `fonts/` (e.g., `fonts/NotoSansCJKsc-Regular.otf`).
  - Add `--burn-fonts-dir fonts --burn-font "Noto Sans CJK SC"`.
- Output files go to `burned/` by default.
 - Burned output format is set via `--burn-format {mp4,webm}` (default: mp4). WebM uses VP9 video + Opus audio (requires ffmpeg with libvpx/libopus).

Auto-defaults
- If a bundled font directory is present at `fonts/Noto_Sans_SC`, the script automatically uses it for burn-in and sets the font family to `Noto Sans SC` when `--burn-font`/`--burn-fonts-dir` are not provided.

Quick usage with auto-detected font

```bash
# If you placed Noto Sans SC under fonts/Noto_Sans_SC (or another supported fonts dir),
# you can just enable burn-in and the script will pick it up automatically.
uv run subtitle-gen --src videos --lang zh --burn-in --burn-use translated --burn-out burned
```

Tip (zsh): If you see `zsh: command not found: --burn-font`, you likely pasted a multi-line command without trailing backslashes. Either keep the backslashes or use the single-line example above.

## Troubleshooting

- "ffmpeg not found": install ffmpeg via brew/choco/apt.
- SDK import errors: ensure `pip install -r requirements.txt`.
- Empty or off timestamps: make sure ASR model supports `verbose_json` with segments (default `whisper-1` does).
  - If using `gpt-4o-transcribe` models, timestamps are not provided by the API; the script will produce a single-segment SRT spanning the file duration.
