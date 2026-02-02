"""Configuration loader for the Telegram bot."""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "api_keys.json"

# User name mapping for time tracking (Telegram username -> Display name)
USER_MAPPING = {
    "andrew": "Andrew",
    "anna": "Anna",
    "audrey": "Audrey",
    "jonathan": "Jonathan",
    "nathaniel": "Nathaniel",
}


def load_config() -> dict:
    """Load configuration from api_keys.json."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {CONFIG_PATH}\n"
            "Please create api_keys.json with your API keys."
        )
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def get_bot_token() -> str:
    """Get the Telegram bot token."""
    config = load_config()
    token = config.get("bot_key", "Secret Key")
    if not token or token == "Secret Key":
        raise ValueError(
            "Please set your Telegram bot token in api_keys.json (bot_key field).\n"
            "Get a token from @BotFather on Telegram."
        )
    return token
