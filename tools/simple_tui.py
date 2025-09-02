#!/usr/bin/env python3
"""
Simple, dependency-free interactive wizard for subtitle-gen.

Run: uv run python tools/simple_tui.py

This mirrors the Rust TUI flow, but uses plain stdin/stdout prompts.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
import termios
import tty
from typing import List, Optional, Tuple
import shutil

if os.name != "nt":  # POSIX-only imports for PTY path
    import pty
    import select
    import fcntl
    import struct


LANG_OPTIONS: List[Tuple[str, str]] = [
    ("Chinese", "zh"),
    ("English", "en"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Spanish", "es"),
    ("French", "fr"),
    ("German", "de"),
    ("Portuguese", "pt"),
    ("No translation", ""),
]


# ---------- Small IO helpers ----------

def green(text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[32m{text}\033[0m"
    return text


def yellow(text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[33m{text}\033[0m"
    return text


def dim(text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[2m{text}\033[0m"
    return text


def q(text: str) -> None:
    print(f"{green('?')} {text}")


def read_key() -> str:
    """Read a single key (supports arrows, Esc, Enter, Space)."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch1 = sys.stdin.read(1)
        if ch1 == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A":
                    return "UP"
                if ch3 == "B":
                    return "DOWN"
                if ch3 == "C":
                    return "RIGHT"
                if ch3 == "D":
                    return "LEFT"
            return "ESC"
        if ch1 in ("\r", "\n"):
            return "ENTER"
        if ch1 == " ":
            return "SPACE"
        return ch1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def choose_keyed(question: str, options: List[str], idx: int = 0, hint: str = "") -> int:
    """Render a simple arrow-key selector in an alternate screen. Returns chosen index."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return prompt_choice(question, options, default_index=idx)

    def enter_alt_screen() -> None:
        # Switch to alternate buffer, clear, hide cursor
        sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?25l")
        sys.stdout.flush()

    def exit_alt_screen() -> None:
        # Restore cursor, return to main buffer
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    try:
        enter_alt_screen()
        while True:
            # Repaint content at the top; clear the rest to avoid residual lines
            sys.stdout.write("\x1b[H\x1b[J")  # move to 1,1 and clear to end of screen
            q(question)
            sys.stdout.write("\n\n")
            for i, opt in enumerate(options):
                if i == idx:
                    sys.stdout.write(f"{yellow('›')} {opt}\n")
                else:
                    sys.stdout.write(f"  {opt}\n")
            if hint:
                sys.stdout.write("\n" + dim(hint))
            sys.stdout.flush()

            key = read_key()
            if key in ("UP", "k"):
                idx = (idx - 1) % len(options)
            elif key in ("DOWN", "j"):
                idx = (idx + 1) % len(options)
            elif key in ("LEFT", "h"):
                idx = max(0, idx - 1)
            elif key in ("RIGHT", "l"):
                idx = min(len(options) - 1, idx + 1)
            elif key == "ENTER":
                return idx
            elif key in ("ESC", "q"):
                sys.exit(0)
    finally:
        exit_alt_screen()


def prompt_choice(label: str, options: List[str], default_index: int | None = None) -> int:
    q(label)
    for i, opt in enumerate(options, start=1):
        mark = " (default)" if default_index is not None and i - 1 == default_index else ""
        print(f"  {i}. {opt}{mark}")
    while True:
        raw = input(yellow("> ")).strip()
        if raw == "" and default_index is not None:
            return default_index
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        print(dim("Please enter a number from the list above."))


def prompt_yes_no(label: str, default: bool | None = None) -> bool:
    suffix = " (y/n)"
    if default is True:
        suffix = " (Y/n)"
    elif default is False:
        suffix = " (y/N)"
    while True:
        raw = input(f"{label}{suffix} ").strip().lower()
        if raw == "" and default is not None:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print(dim("Please answer 'y' or 'n'."))


def prompt_text(label: str, default: str | None = None) -> str:
    # Show one-line hint for text input
    print(dim("Type to edit. Enter to continue. Ctrl+C to quit."))
    if default:
        raw = input(f"{label} [{default}] ").strip()
        return raw or default
    return input(f"{label} ").strip()


# ---------- Command building ----------

def find_pyproject_dir(start: Path) -> Optional[Path]:
    cur = start
    for _ in range(6):
        if (cur / "pyproject.toml").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def build_command(
    mode_local: bool,
    src_path: str,
    yt_url: str | None,
    lang_code: str,
    overwrite: bool,
    burn_in: bool,
    burn_use: str | None,
    burn_format: str | None,
) -> Tuple[str, List[str], Optional[Path]]:
    repo = find_pyproject_dir(Path.cwd())
    program = "uv" if repo else "subtitle-gen"
    args: List[str] = []
    if repo:
        args += ["run", "subtitle-gen"]

    if mode_local:
        args += ["--src", src_path]
    else:
        if yt_url:
            args += ["--yt", yt_url]
        args += ["--src", src_path]

    if lang_code:
        args += ["--lang", lang_code]

    if overwrite:
        args.append("--overwrite")

    if burn_in:
        args += [
            "--burn-in",
            "--burn-use",
            burn_use or "translated",
            "--burn-format",
            burn_format or "mp4",
        ]

    return program, args, repo


# ---------- Runner ----------

def run_and_stream(program: str, args: List[str], cwd: Optional[Path]) -> int:
    # Prefer PTY on POSIX so the child believes it has a TTY (enables colors)
    if os.name != "nt":
        try:
            return _run_with_pty(program, args, cwd)
        except Exception:
            # Fallback to piped mode if PTY fails
            pass
    print()
    print("Running:")
    print("$", program, *[sh_quote(a) for a in args])
    print()

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("FORCE_COLOR", "1")

    preexec = os.setsid if hasattr(os, "setsid") else None
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        proc = subprocess.Popen(
            [program, *args],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
            text=False,
            universal_newlines=False,
            preexec_fn=preexec,  # type: ignore[arg-type]
            creationflags=creationflags,
        )
    except FileNotFoundError:
        print(f"Error: failed to spawn '{program}'. Is it installed?")
        return 127

    interrupted = {"count": 0}
    suppress_traceback = {"on": False, "in_tb": False}

    def handle_sigint(_sig, _frm):
        interrupted["count"] += 1
        if interrupted["count"] == 1:
            sys.stderr.write("\n[ctrl-c] Stopping… (press again to force kill)\n")
            sys.stderr.flush()
            suppress_traceback["on"] = True
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGTERM)  # type: ignore[arg-type]
                else:
                    proc.terminate()
            except Exception:
                pass
        else:
            try:
                proc.kill()
            except Exception:
                pass

    signal.signal(signal.SIGINT, handle_sigint)

    assert proc.stdout is not None
    # CR-aware streaming: replace last line on '\r'; append on '\n'
    last_len = 0
    try:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            acc = ""
            for ch in text:
                if ch == "\r":
                    # erase current line and write updated content
                    # Only echo if not suppressing traceback
                    if not suppress_traceback["on"] or not suppress_traceback["in_tb"]:
                        sys.stdout.write("\r" + acc)
                        sys.stdout.flush()
                    last_len = len(acc)
                    acc = ""
                elif ch == "\n":
                    # finalize the line
                    # clear any residual characters from previous longer line
                    clear = " " * max(0, last_len - len(acc))
                    line = acc
                    # Optional traceback suppression after Ctrl+C
                    if suppress_traceback["on"]:
                        if line.startswith("Traceback (most recent call last):"):
                            suppress_traceback["in_tb"] = True
                            line = ""
                        elif "KeyboardInterrupt" in line:
                            suppress_traceback["in_tb"] = False
                            line = ""
                        elif suppress_traceback["in_tb"]:
                            line = ""
                    if line:
                        sys.stdout.write("\r" + line + clear + "\n")
                        sys.stdout.flush()
                    last_len = 0
                    acc = ""
                else:
                    acc += ch
            if acc:
                # show partial line in-place
                clear = " " * max(0, last_len - len(acc))
                if not suppress_traceback["on"] or not suppress_traceback["in_tb"]:
                    sys.stdout.write("\r" + acc + clear)
                    sys.stdout.flush()
                last_len = len(acc)
    finally:
        proc.wait()
        sys.stdout.write("\n[done] Exit code: %d\n" % proc.returncode)
        sys.stdout.flush()
    return int(proc.returncode or 0)


def _set_winsize(fd: int) -> None:
    try:
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass


def _run_with_pty(program: str, args: List[str], cwd: Optional[Path]) -> int:
    print()
    print("Running:")
    print("$", program, *[sh_quote(a) for a in args])
    print()

    env = os.environ.copy()
    # Child should colorize because stdout is a TTY; still set helpful envs
    env.pop("NO_COLOR", None)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("TERM", env.get("TERM", "xterm-256color"))

    pid, fd = pty.fork()
    if pid == 0:
        # child
        try:
            if cwd:
                os.chdir(str(cwd))
            os.execvpe(program, [program, *args], env)
        except Exception:
            os._exit(127)

    # parent
    _set_winsize(fd)

    interrupted = {"count": 0}

    def handle_sigint(_sig, _frm):
        interrupted["count"] += 1
        if interrupted["count"] == 1:
            sys.stderr.write("\n[ctrl-c] Stopping… (press again to force kill)\n")
            sys.stderr.flush()
            try:
                os.killpg(pid, signal.SIGTERM)  # graceful
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass

    old_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    try:
        # Stream bytes directly, preserving ANSI and carriage returns
        while True:
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd in r:
                try:
                    data = os.read(fd, 8192)
                except OSError:
                    data = b""
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            # Check if child exited
            try:
                pid_done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pid_done = pid
                status = 0
            if pid_done == pid:
                code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 130
                print(f"\n[done] Exit code: {code}")
                return code
    finally:
        signal.signal(signal.SIGINT, old_handler)
    # If loop exits without waitpid catching, do a blocking wait
    _, status = os.waitpid(pid, 0)
    code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 130
    print(f"\n[done] Exit code: {code}")
    return code


def sh_quote(s: str) -> str:
    if not s or any(c in s for c in " \"'$"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


# ---------- Main Wizard ----------

def main() -> int:
    print(green("subtitle-tui"), "—", "interactive wizard (simple)")

    # Q1 — Mode
    mode_idx = choose_keyed(
        "How do you want to get videos?",
        ["Use a local folder", "Download from YouTube"],
        idx=0,
        hint="Use Up/Down to choose. Enter to continue. Ctrl+C to quit.",
    )
    mode_local = (mode_idx == 0)

    # Q2 — Paths/URL
    if mode_local:
        src_path = prompt_text("Where are the videos located?", default="videos")
        yt_url = None
    else:
        src_path = prompt_text("Where should downloaded videos be saved?", default="videos")
        yt_url = prompt_text("Paste a YouTube URL (single)", default="").strip() or None

    # Q3 — Language
    lang_idx = choose_keyed(
        "What’s the target language for the subtitles?",
        [f"{label} ({code})" for label, code in LANG_OPTIONS],
        idx=0,
        hint="Use Up/Down to select. Enter to continue. Ctrl+C to quit.",
    )
    lang_code = LANG_OPTIONS[lang_idx][1]

    # Q4 — Overwrite
    overwrite = prompt_yes_no("Overwrite existing results if found?", default=False)

    # Q5 — Burn-in
    burn_in = prompt_yes_no("Do you want to burn subtitles into the video?", default=False)

    burn_use = None
    burn_format = None
    if burn_in:
        burn_use_idx = choose_keyed(
            "Which subtitles should be burned?",
            ["Translated", "Original"],
            idx=0,
            hint="Left/Right to choose. Enter to continue. Ctrl+C to quit.",
        )
        burn_use = ["translated", "original"][burn_use_idx]
        burn_fmt_idx = choose_keyed(
            "What output format for the burned video?",
            ["MP4", "WebM"],
            idx=0,
            hint="Left/Right to choose. Enter to continue. Ctrl+C to quit.",
        )
        burn_format = ["mp4", "webm"][burn_fmt_idx]

    program, args, cwd = build_command(mode_local, src_path, yt_url, lang_code, overwrite, burn_in, burn_use, burn_format)

    print()
    print("Summary")
    print("- Mode:", "Local folder" if mode_local else "YouTube")
    print("- Source path:", src_path)
    if yt_url:
        print("- YouTube URL:", yt_url)
    print("- Language:", lang_code or "(none)")
    print("- Overwrite:", "Yes" if overwrite else "No")
    print("- Burn-in:", "Yes" if burn_in else "No")
    if burn_in:
        print("  - Burn use:", burn_use)
        print("  - Burn format:", burn_format)

    print()
    print("Command preview:")
    print("$", program, *[sh_quote(a) for a in args])

    if not prompt_yes_no("Run now?", default=True):
        print("Canceled.")
        return 0

    return run_and_stream(program, args, cwd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
