import os

from dotenv import load_dotenv

from yeoman.cli.commands import app

# Load .env file from ~/.yeoman/ if it exists
# Precedence: existing env vars > .env file (override=False)
load_dotenv(os.path.expanduser("~/.yeoman/.env"), override=False)

if __name__ == "__main__":
    app()
