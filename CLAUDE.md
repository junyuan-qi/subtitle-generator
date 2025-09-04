# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

**Environment Setup:**
```bash
uv sync                    # Install deps and create .venv from pyproject.toml/uv.lock
```

**Running the Application:**
```bash
uv run subtitle-gen --help              # Show CLI help
uv run subtitle-gen --src videos --lang zh    # Basic usage
uv run main.py --src videos --lang zh         # Wrapper with --burn-in enabled by default
cd tui && cargo run --release           # Interactive TUI wizard (generates and runs CLI command)
```

**Prerequisites Check:**
```bash
ffmpeg -version           # Verify ffmpeg is available (required for audio extraction)
```

**Testing:**
```bash
uv run pytest -q         # Run tests (framework: pytest, location: tests/)
```

**Linting:**
```bash
uv run ruff check         # Run linting checks (development dependency)
uv run ruff format        # Apply code formatting
```

**Build:**
```bash
uv build                 # Build wheel using Hatchling
```

## Architecture Overview

**Core Processing Pipeline:**
1. **Video Input** → **Audio Extraction** (ffmpeg) → **Transcription** (OpenAI) → **Translation** (Gemini) → **Optional Burn-in** (ffmpeg)
2. Supports batch processing of multiple videos in a source directory
3. Each step creates intermediate files that can be skipped if they exist (unless `--overwrite`)

**Module Structure:**
- `tools/subtitle_gen.py` - Main CLI orchestration, argument parsing, and processing pipeline
- `tools/ffmpeg_utils.py` - ffmpeg operations (audio extraction, subtitle burn-in, font detection)
- `tools/fs_utils.py` - Filesystem utilities (directory creation, video file discovery)
- `main.py` - Thin wrapper that enables `--burn-in` by default

**Key Dependencies:**
- `openai` - Speech-to-text transcription via Whisper models
- `google-genai` - Translation via Gemini models  
- `python-dotenv` - Environment variable loading from `.env`
- `ruff` - Fast Python linter and formatter (development dependency)
- External: `ffmpeg` (audio/video processing), `yt-dlp` (optional YouTube downloading)

**Data Flow:**
```
videos/ → audio/*.wav → subs/*.srt → subs_zh/*.{lang}.srt → burned/*.burned.{mp4|webm}
```

## Configuration & Environment

**Required Environment Variables** (place in `.env`):
```
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...     # or GEMINI_API_KEY
```

**Auto-detected Configurations:**
- Font bundling: Checks `fonts/Noto_Sans_SC/` for CJK subtitle burn-in
- Path sanitization: Replaces invalid filename characters with underscores
- Model normalization: Maps common aliases (e.g., `gemini-flash` → `gemini-2.5-flash`)

## Key CLI Patterns

**Provider & Model Selection:**
- ASR: `--asr-provider openai --asr-model whisper-1` (timestamps) or `gpt-4o-transcribe` (text-only)
- Translation: `--tx-provider gemini --tx-model gemini-2.5-flash`

**Burn-in Subtitle Options:**
- `--burn-use translated` (default) or `original`
- `--burn-font "PingFang SC" --burn-font-size 28 --burn-margin-v 40`
- `--burn-format mp4` (H.264) or `webm` (VP9+Opus)
- `--burn-fonts-dir fonts/` for custom font directories

**YouTube Integration:**
- `--yt "https://youtu.be/ID"` (repeat for multiple URLs)
- `--yt-format "bv*+ba/best" --yt-output-tmpl "%(title).200B.%(ext)s"`

## Error Handling Patterns

**Common Failure Points:**
- Missing `ffmpeg` or `yt-dlp` on PATH → Check availability before processing
- Missing API keys → Runtime error with clear message
- Invalid video files → Graceful skip with warning
- API rate limits → No built-in retry logic (improvement opportunity)

**File Management:**
- Creates output directories automatically (`audio/`, `subs/`, etc.)
- Skips existing files unless `--overwrite` specified
- Sanitizes filenames to avoid path traversal issues

## Development Considerations

**Extensibility Points:**
- New ASR providers: Extend `--asr-provider` choices and add implementation
- New translation providers: Extend `--tx-provider` choices and add implementation  
- Additional subtitle formats: Currently SRT-only but parse_srt/write_srt are modular

**Testing Gaps:** 
- No existing test coverage for 1,347+ line codebase
- Critical functions lack error scenario testing (API failures, malformed responses)
- Missing integration tests for full pipeline

**Performance Considerations:**
- Processes videos sequentially (not parallel)
- Loads entire SRT files into memory for translation
- No chunking for very large audio files
- API calls are synchronous with no rate limiting

**Security Notes:**
- API keys auto-loaded from `.env` (not committed)
- Basic filename sanitization prevents directory traversal
- No input validation on URLs or file paths beyond existence checks