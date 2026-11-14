import pytest
from pytest_django import Settings


@pytest.fixture(autouse=True)
def keys(settings: Settings) -> None:
    """TextModels needs the key in the settings; the fake models never use it."""
    settings.GEMINI_API_KEY = "gemini-test-key"
