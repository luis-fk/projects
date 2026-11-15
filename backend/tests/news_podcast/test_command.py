from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django import Settings

from news_podcast.api.core.schemas import NewsStory, Usage
from news_podcast.api.core.telegram import admin_chat_id
from news_podcast.api.episode import run
from news_podcast.api.episode.run import EpisodeOutcome, Selection
from news_podcast.api.lesson import run as lesson_run
from news_podcast.api.lesson.state import LessonError
from news_podcast.models import SettingsMissing


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


def record_runs(
    monkeypatch: pytest.MonkeyPatch,
    news: EpisodeOutcome | Exception,
    lesson: Exception | None = None,
    lesson_raises: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    """The episode action's calls to make_episode and make_lesson, in order."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def make_episode(**options: Any) -> EpisodeOutcome:
        calls.append(("episode", options))
        if isinstance(news, Exception):
            raise news
        return news

    def make_lesson(stories: list[NewsStory], **options: Any) -> Exception | None:
        calls.append(("lesson", {"stories": stories, **options}))
        if lesson_raises and lesson:
            raise lesson
        return lesson

    monkeypatch.setattr(run, "make_episode", make_episode)
    monkeypatch.setattr(lesson_run, "make_lesson", make_lesson)
    return calls


SENT = EpisodeOutcome(paused=False, sent=True, stories=STORIES, error=None)


def test_without_the_option_the_episode_runs_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record_runs(monkeypatch, SENT)

    call_command("news_podcast", "episode", "--scheduled")

    assert calls == [("episode", {"scheduled": True, "send": True})]


@pytest.mark.parametrize("value", ["0", "-1", "dois", "1.5"])
def test_the_option_refuses_anything_but_a_whole_number_from_one(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    calls = record_runs(monkeypatch, SENT)

    with pytest.raises(CommandError, match="--lesson-every-days"):
        call_command("news_podcast", "episode", "--lesson-every-days", value)
    assert calls == []


@pytest.mark.parametrize(
    ("args", "scheduled", "send"),
    [
        (["--scheduled"], True, True),
        ([], False, True),
        (["--scheduled", "--no-send"], True, False),
    ],
)
@pytest.mark.parametrize("days", [1, 2])
def test_the_lesson_follows_the_episode_with_the_runs_options(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    scheduled: bool,
    send: bool,
    days: int,
) -> None:
    calls = record_runs(monkeypatch, SENT)

    call_command("news_podcast", "episode", *args, "--lesson-every-days", str(days))

    assert calls == [
        ("episode", {"scheduled": scheduled, "send": send}),
        (
            "lesson",
            {
                "stories": STORIES,
                "send": send,
                "news_sent": True,
                "scheduled": scheduled,
                "every_days": days,
            },
        ),
    ]


@pytest.mark.parametrize("stories", [STORIES, []])
def test_after_a_failed_episode_the_lesson_gets_its_stories_and_no_news(
    monkeypatch: pytest.MonkeyPatch, stories: list[NewsStory]
) -> None:
    failed = EpisodeOutcome(
        paused=False, sent=False, stories=stories, error=LessonError("a voz caiu")
    )
    calls = record_runs(monkeypatch, failed)

    with pytest.raises(CommandError, match="^episódio: a voz caiu$"):
        call_command("news_podcast", "episode", "--lesson-every-days", "2")

    [_, (_, lesson)] = calls
    assert (lesson["stories"], lesson["news_sent"]) == (stories, False)


def test_a_paused_run_makes_no_lesson(monkeypatch: pytest.MonkeyPatch) -> None:
    paused = EpisodeOutcome(paused=True, sent=False, stories=[], error=None)
    calls = record_runs(monkeypatch, paused)

    call_command("news_podcast", "episode", "--scheduled", "--lesson-every-days", "2")

    assert [name for name, _ in calls] == ["episode"]


@pytest.mark.parametrize(
    ("news_error", "lesson_error", "lesson_raises", "message"),
    [
        (LessonError("x"), LessonError("y"), False, "episódio: x; aula: y"),
        (None, LessonError("y"), False, "aula: y"),
        (
            LessonError("x"),
            LessonError("o banco caiu"),
            True,
            "episódio: x; aula: o banco caiu",
        ),
    ],
)
def test_the_command_names_each_failed_part(
    monkeypatch: pytest.MonkeyPatch,
    news_error: Exception | None,
    lesson_error: Exception,
    lesson_raises: bool,
    message: str,
) -> None:
    news = EpisodeOutcome(paused=False, sent=False, stories=[], error=news_error)
    record_runs(monkeypatch, news, lesson_error, lesson_raises)

    with pytest.raises(CommandError) as raised:
        call_command("news_podcast", "episode", "--lesson-every-days", "2")
    assert str(raised.value) == message


def test_both_parts_succeeding_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_runs(monkeypatch, SENT)

    call_command("news_podcast", "episode", "--scheduled", "--lesson-every-days", "2")

    assert [name for name, _ in calls] == ["episode", "lesson"]


def test_a_settings_failure_of_the_episode_raises_with_no_lesson(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record_runs(monkeypatch, SettingsMissing("rode seed_news_podcast"))

    with pytest.raises(CommandError, match="rode seed_news_podcast"):
        call_command(
            "news_podcast", "episode", "--scheduled", "--lesson-every-days", "2"
        )
    assert [name for name, _ in calls] == ["episode"]


@pytest.mark.parametrize("action", ["lesson", "chat-id"])
def test_the_cadence_goes_only_with_the_episode_action(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    calls = record_runs(monkeypatch, SENT)

    with pytest.raises(CommandError, match="--lesson-every-days"):
        call_command("news_podcast", action, "--lesson-every-days", "2")
    assert calls == []
