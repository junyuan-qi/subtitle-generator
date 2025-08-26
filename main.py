import sys
from tools.subtitle_gen import main as cli_main


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))

