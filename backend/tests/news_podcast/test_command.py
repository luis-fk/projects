from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django import Settings

from news_podcast.api.core.schemas import NewsStory, Usage
from news_podcast.api.core.telegram import admin_chat_id
from news_podcast.api.episode import run
from news_podcast.api.episode.run import Selection
from news_podcast.api.lesson import run as lesson_run
from news_podcast.api.lesson.state import LessonError


@pytest.fixture(autouse=True)
def no_keys(settings: Settings) -> None:
    """Keeps the tests away from the models and Telegram, even with keys in the .env."""
    settings.GEMINI_API_KEY = None
    settings.OPENAI_API_KEY = None
    settings.TELEGRAM_BOT_TOKEN = None
    settings.TELEGRAM_CHAT_ID = None
    settings.TELEGRAM_ADMIN_CHAT_ID = None


def test_without_an_admin_chat_the_notices_go_to_the_episode_chat(
    settings: Settings,
) -> None:
    settings.TELEGRAM_CHAT_ID = "-22"

    assert admin_chat_id() == "-22"

    settings.TELEGRAM_ADMIN_CHAT_ID = "11"
    assert admin_chat_id() == "11"


def test_the_chat_id_action_fails_without_the_bot_token() -> None:
    with pytest.raises(CommandError, match="TELEGRAM_BOT_TOKEN não configurado"):
        call_command("news_podcast", "chat-id")


STORIES = [NewsStory("Congresso derruba veto", "politica", "como o veto caiu")]
SELECTION_USAGES = [Usage(step="selection", model="gemini-text", input_tokens=10)]


def record_lesson(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None = None
) -> list[tuple[list[NewsStory], dict[str, Any]]]:
    """The lesson action's calls to make_lesson, after a selection of STORIES."""
    calls: list[tuple[list[NewsStory], dict[str, Any]]] = []

    def make_lesson(stories: list[NewsStory], **options: Any) -> Exception | None:
        calls.append((stories, options))
        return error

    monkeypatch.setattr(
        run, "select_today", lambda: Selection(STORIES, SELECTION_USAGES, None)
    )
    monkeypatch.setattr(lesson_run, "make_lesson", make_lesson)
    return calls


@pytest.mark.parametrize(("args", "send"), [([], True), (["--no-send"], False)])
def test_the_lesson_action_makes_a_lesson_on_the_days_selection(
    monkeypatch: pytest.MonkeyPatch, args: list[str], send: bool
) -> None:
    calls = record_lesson(monkeypatch)

    call_command("news_podcast", "lesson", *args)

    assert calls == [
        (
            STORIES,
            {"send": send, "news_sent": False, "usages": SELECTION_USAGES},
        )
    ]


def test_a_failed_lesson_fails_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    record_lesson(monkeypatch, LessonError("Nenhum tema disponível"))

    with pytest.raises(CommandError, match="Nenhum tema disponível"):
        call_command("news_podcast", "lesson")
