import pytest
from pytest_django import Settings


@pytest.fixture(autouse=True)
def keys(settings: Settings) -> None:
    """TextModels needs the key in the settings; the fake models never use it. The
    other keys stay off, so no test reaches Tavily, OpenAI or Telegram even with keys
    in the .env."""
    settings.GEMINI_API_KEY = "gemini-test-key"
    settings.OPENAI_API_KEY = None
    settings.TAVILY_API_KEY = None
    settings.TELEGRAM_BOT_TOKEN = None
    settings.TELEGRAM_CHAT_ID = None
    settings.TELEGRAM_ADMIN_CHAT_ID = None
