import os

from dotenv import load_dotenv

from nanobot.cli.commands import app

# Load .env file from ~/.nanobot/ if it exists
# Precedence: existing env vars > .env file (override=False)
load_dotenv(os.path.expanduser("~/.nanobot/.env"), override=False)

if __name__ == "__main__":
    app()
