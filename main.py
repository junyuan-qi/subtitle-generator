import sys
from tools.subtitle_gen import main as cli_main


if __name__ == "__main__":
    args = sys.argv[1:]
    # When running the module directly (e.g., `uv run main.py`),
    # enable burn-in by default unless user explicitly asks for help
    # or already provided --burn-in.
    if args and any(a in ("-h", "--help") for a in args):
        pass
    elif "--burn-in" not in args:
        args = ["--burn-in", *args]
    raise SystemExit(cli_main(args))
