from dotenv import load_dotenv
from yeoman_shared.utils.helpers import resolve_yeoman_path

# Load .env file from ~/.yeoman/ if it exists
# Precedence: existing env vars > .env file (override=False)
load_dotenv(resolve_yeoman_path("~/.yeoman/.env", is_default=True), override=False)

from yeoman_gateway.cli.commands import app  # noqa: E402


def main() -> None:
    app()


if __name__ == "__main__":
    main()
