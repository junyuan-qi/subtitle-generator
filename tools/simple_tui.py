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


def _handle_navigation_key(key: str, idx: int, max_index: int) -> int | None:
    """Handle navigation keys and return new index, or None for non-nav keys."""
    if key in ("UP", "k"):
        return (idx - 1) % (max_index + 1)
    elif key in ("DOWN", "j"):
        return (idx + 1) % (max_index + 1)
    elif key in ("LEFT", "h"):
        return max(0, idx - 1)
    elif key in ("RIGHT", "l"):
        return min(max_index, idx + 1)
    return None


def _render_menu(question: str, options: List[str], idx: int, hint: str) -> None:
    """Render the menu options with current selection highlighted."""
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


def choose_keyed(
    question: str, options: List[str], idx: int = 0, hint: str = ""
) -> int:
    """Render a simple arrow-key selector in an alternate screen. Returns chosen index."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return prompt_choice(question, options, default_index=idx)

    def enter_alt_screen() -> None:
        sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?25l")
        sys.stdout.flush()

    def exit_alt_screen() -> None:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    try:
        enter_alt_screen()
        while True:
            _render_menu(question, options, idx, hint)
            key = read_key()

            new_idx = _handle_navigation_key(key, idx, len(options) - 1)
            if new_idx is not None:
                idx = new_idx
            elif key == "ENTER":
                return idx
            elif key in ("ESC", "q"):
                sys.exit(0)
    finally:
        exit_alt_screen()


def prompt_choice(
    label: str, options: List[str], default_index: int | None = None
) -> int:
    q(label)
    for i, opt in enumerate(options, start=1):
        mark = (
            " (default)" if default_index is not None and i - 1 == default_index else ""
        )
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


def _setup_process_env() -> dict:
    """Set up environment variables for subprocess."""
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("FORCE_COLOR", "1")
    return env


def _get_process_flags():
    """Get platform-specific process creation flags."""
    preexec = os.setsid if hasattr(os, "setsid") else None
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return preexec, creationflags


def _create_subprocess(program: str, args: List[str], cwd: Optional[Path]):
    """Create and return a subprocess.Popen instance."""
    env = _setup_process_env()
    preexec, creationflags = _get_process_flags()

    return subprocess.Popen(
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


def _setup_signal_handler(proc, suppress_traceback):
    """Set up SIGINT handler for graceful process termination."""
    interrupted = {"count": 0}

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
    return interrupted


def _process_traceback_line(line: str, suppress_traceback: dict) -> str:
    """Filter out traceback lines if suppression is enabled."""
    if not suppress_traceback["on"]:
        return line

    if line.startswith("Traceback (most recent call last):"):
        suppress_traceback["in_tb"] = True
        return ""
    elif "KeyboardInterrupt" in line:
        suppress_traceback["in_tb"] = False
        return ""
    elif suppress_traceback["in_tb"]:
        return ""
    return line


def _stream_output(proc, suppress_traceback):
    """Stream process output with CR-aware line handling."""
    last_len = 0
    acc = ""

    while True:
        chunk = proc.stdout.read(8192)
        if not chunk:
            break

        text = chunk.decode(errors="replace")

        for ch in text:
            if ch == "\r":
                if not suppress_traceback["on"] or not suppress_traceback["in_tb"]:
                    sys.stdout.write("\r" + acc)
                    sys.stdout.flush()
                last_len = len(acc)
                acc = ""
            elif ch == "\n":
                clear = " " * max(0, last_len - len(acc))
                line = _process_traceback_line(acc, suppress_traceback)
                if line:
                    sys.stdout.write("\r" + line + clear + "\n")
                    sys.stdout.flush()
                last_len = 0
                acc = ""
            else:
                acc += ch

        if acc:
            clear = " " * max(0, last_len - len(acc))
            if not suppress_traceback["on"] or not suppress_traceback["in_tb"]:
                sys.stdout.write("\r" + acc + clear)
                sys.stdout.flush()
            last_len = len(acc)


def run_and_stream(program: str, args: List[str], cwd: Optional[Path]) -> int:
    """Run a program and stream its output with CR-aware line handling."""
    if os.name != "nt":
        try:
            return _run_with_pty(program, args, cwd)
        except Exception:
            pass

    print()
    print("Running:")
    print("$", program, *[sh_quote(a) for a in args])
    print()

    try:
        proc = _create_subprocess(program, args, cwd)
    except FileNotFoundError:
        print(f"Error: failed to spawn '{program}'. Is it installed?")
        return 127

    suppress_traceback = {"on": False, "in_tb": False}
    _setup_signal_handler(proc, suppress_traceback)

    assert proc.stdout is not None
    try:
        _stream_output(proc, suppress_traceback)
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


def _setup_pty_env() -> dict:
    """Set up environment variables for PTY subprocess."""
    env = os.environ.copy()
    env.pop("NO_COLOR", None)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("TERM", env.get("TERM", "xterm-256color"))
    return env


def _kill_process(pid: int, signal_type: int) -> None:
    """Kill process with fallback from process group to individual process."""
    try:
        os.killpg(pid, signal_type)
    except Exception:
        try:
            os.kill(pid, signal_type)
        except Exception:
            pass


def _setup_pty_signal_handler(pid: int):
    """Set up SIGINT handler for PTY process."""
    interrupted = {"count": 0}

    def handle_sigint(_sig, _frm):
        interrupted["count"] += 1
        if interrupted["count"] == 1:
            sys.stderr.write("\n[ctrl-c] Stopping… (press again to force kill)\n")
            sys.stderr.flush()
            _kill_process(pid, signal.SIGTERM)
        else:
            _kill_process(pid, signal.SIGKILL)

    old_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    return old_handler


def _stream_pty_output(fd: int, pid: int) -> int | None:
    """Stream PTY output and check for process completion."""
    r, _, _ = select.select([fd], [], [], 0.1)
    if fd in r:
        try:
            data = os.read(fd, 8192)
        except OSError:
            data = b""
        if not data:
            return None
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    try:
        pid_done, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pid_done = pid
        status = 0

    if pid_done == pid:
        return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 130
    return None


def _run_with_pty(program: str, args: List[str], cwd: Optional[Path]) -> int:
    print()
    print("Running:")
    print("$", program, *[sh_quote(a) for a in args])
    print()

    env = _setup_pty_env()
    pid, fd = pty.fork()

    if pid == 0:
        try:
            if cwd:
                os.chdir(str(cwd))
            os.execvpe(program, [program, *args], env)
        except Exception:
            os._exit(127)

    _set_winsize(fd)
    old_handler = _setup_pty_signal_handler(pid)

    try:
        while True:
            exit_code = _stream_pty_output(fd, pid)
            if exit_code is not None:
                print(f"\n[done] Exit code: {exit_code}")
                return exit_code
    finally:
        signal.signal(signal.SIGINT, old_handler)

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
    mode_local = mode_idx == 0

    # Q2 — Paths/URL
    if mode_local:
        src_path = prompt_text("Where are the videos located?", default="videos")
        yt_url = None
    else:
        src_path = prompt_text(
            "Where should downloaded videos be saved?", default="videos"
        )
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
    burn_in = prompt_yes_no(
        "Do you want to burn subtitles into the video?", default=False
    )

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

    program, args, cwd = build_command(
        mode_local,
        src_path,
        yt_url,
        lang_code,
        overwrite,
        burn_in,
        burn_use,
        burn_format,
    )

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
