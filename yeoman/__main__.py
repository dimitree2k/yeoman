import os

from dotenv import load_dotenv

# Load .env file from ~/.yeoman/ if it exists
# Precedence: existing env vars > .env file (override=False)
load_dotenv(os.path.expanduser("~/.yeoman/.env"), override=False)

from yeoman.cli.commands import app  # noqa: E402


def main() -> None:
    app()


if __name__ == "__main__":
    main()
