"""Load settings from config.toml and secrets from .env."""

import tomllib  # built into Python 3.11+
from pathlib import Path

from dotenv import load_dotenv

# The project folder is the parent of this package folder.
PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_config(path: Path | None = None) -> dict:
    """Read config.toml into a dictionary and load .env into environment variables."""
    path = path or PROJECT_DIR / "config.toml"
    with open(path, "rb") as f:  # tomllib needs the file opened in binary mode
        config = tomllib.load(f)

    # Secrets become environment variables (os.environ), so they never sit in config.
    load_dotenv(PROJECT_DIR / ".env")

    # Make the data and log folders absolute paths inside the project and make sure they exist.
    for key in ("data_dir", "log_dir"):
        folder = PROJECT_DIR / config["paths"][key]
        folder.mkdir(parents=True, exist_ok=True)
        config["paths"][key] = folder
    return config
