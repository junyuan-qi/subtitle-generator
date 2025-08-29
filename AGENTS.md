# Repository Guidelines

## Project Structure & Module Organization
- `main.py`: Thin wrapper that forwards to the CLI entry.
- `tools/`: Core CLI logic in `subtitle_gen.py` and package entrypoint `subtitle-gen`.
- `fonts/`: Optional bundled fonts for burn‑in (e.g., `fonts/Noto_Sans_SC`).
- Generated at runtime: `audio/`, `subs/`, `subs_zh/`, `burned/`.
- Config: `.env.example` documents required keys; `.env` is auto‑loaded.
- Tests (if added): under `tests/` with `test_*.py` files.

## Build, Test, and Development Commands
- `uv sync`: Create `.venv` and install deps from `pyproject.toml`/`uv.lock`.
- `uv run subtitle-gen --src videos --lang zh`: Run CLI (preferred).
- `uv run python main.py --src videos --lang zh`: Run via Python wrapper.
- `uv build`: Build a wheel (Hatchling).
- `ffmpeg -version`: Verify `ffmpeg` is on `PATH` before running.

## CLI Help Snapshot
- Usage: `subtitle-gen [options]` — view all flags with `uv run subtitle-gen --help`.
- Key flags: `--src`, `--lang`, `--overwrite`, `--burn-in`, `--burn-use {translated|original}`, `--burn-format {mp4|webm}`.
- Burn-in options: `--burn-font`, `--burn-font-size`, `--burn-margin-v`, `--burn-fonts-dir`.
- Providers: `--asr-provider openai` (`--asr-model whisper-1 | gpt-4o*-transcribe`), `--tx-provider gemini` (`--tx-model gemini-2.5-*`).
- yt-dlp: `--yt <url>`, `--yt-format bv*+ba/best`, `--yt-output-tmpl %(title).200B.%(ext)s`, `--yt-quiet`.

## Coding Style & Naming Conventions
- Language: Python ≥ 3.13; 4‑space indentation; follow PEP 8.
- Use type hints where practical; descriptive names; avoid one‑letter identifiers.
- Files/modules: `snake_case` (e.g., `subtitle_gen.py`).
- CLI flags: `--kebab-case` (e.g., `--burn-in`, `--burn-font`).
- Keep functions focused; prefer pure helpers in `tools/`.

## Testing Guidelines
- Framework: `pytest`.
- Location: `tests/` with `test_*.py` naming.
- Coverage focus: happy‑path and error‑path (e.g., missing `ffmpeg`, missing API keys).
- Run: `uv run pytest -q`.

## Commit & Pull Request Guidelines
- Commits: Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`). Keep small and scoped; include rationale when behavior changes.
- PRs: Provide clear description, reproduction steps, and before/after notes. Link issues when applicable and include screenshots/logs for user‑visible changes.
- Verify CLI help stays accurate: `uv run subtitle-gen --help`.

## Security & Configuration Tips
- Do not commit secrets. Copy `.env.example` → `.env` and set `OPENAI_API_KEY`, `GOOGLE_API_KEY` (or `GEMINI_API_KEY`).
- Validate `ffmpeg` locally before processing media.
- For CJK burn‑in, prefer bundled fonts or specify `--burn-font` and `--burn-fonts-dir`; choose source with `--burn-use translated` when needed.

## Common Recipes
- Transcribe + translate: `uv run subtitle-gen --src videos --lang zh`.
- Overwrite and burn translated: `uv run subtitle-gen --src videos --lang zh --overwrite --burn-in --burn-use translated`.
- Burn original SRT: `uv run subtitle-gen --src videos --burn-in --burn-use original`.
- Download then process: `uv run subtitle-gen --yt "https://youtu.be/ID" --src videos --lang zh` (repeat `--yt` for multiple URLs).
- Specify CJK font: `uv run subtitle-gen --burn-in --burn-font NotoSansSC-Regular --burn-fonts-dir fonts/Noto_Sans_SC`.
- WebM output: add `--burn-format webm`; quieter yt-dlp: add `--yt-quiet`.
