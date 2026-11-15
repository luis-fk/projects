import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from news_podcast.api.core.config import HOSTS
from news_podcast.api.core.schemas import Turn, Usage
from news_podcast.api.core.voice.base import VoiceProvider, VoiceUnavailableError
from news_podcast.api.lesson import delivery
from news_podcast.api.lesson.delivery import build_audio, narrate_lesson, send_lesson
from news_podcast.api.lesson.graph import Node
from news_podcast.api.lesson.schemas import LessonPage, TopicToTry
from news_podcast.api.lesson.state import LessonContext, LessonError, LessonState
from tests.news_podcast.factories import LESSON_SETTINGS, build_lesson_options
from tests.news_podcast.fake_voice import FakeVoice
from tests.news_podcast.lesson.context import (
    CATALOG,
    STORIES,
    TODAY,
    VETO,
    FakeVoiceNote,
    RecordingTelegram,
    lesson_context,
    offline_client,
)

TURNS = [Turn(speaker="Bia", text="O veto é a recusa."), Turn(speaker="Léo", text="E?")]
PAGES = [
    LessonPage(
        title="Entenda o veto",
        site="www12.senado.leg.br",
        url="https://www12.senado.leg.br/noticias/entenda-o-veto",
        published=None,
        excerpt="",
        text="",
    )
]
SENT_STATE: LessonState = {
    "topic": TopicToTry(VETO, STORIES[1]),
    "pages": PAGES,
    "audio": Path("aula.ogg"),
    "duration_seconds": 545.6,
}
MakeContext = Callable[[httpx.AsyncClient], LessonContext]


def run_node(
    node: Node, state: LessonState, make_context: MakeContext
) -> tuple[LessonState, LessonContext]:
    """Runs one node over a context built inside the run, as the lesson builds its
    clients."""

    async def scenario() -> tuple[LessonState, LessonContext]:
        async with offline_client() as client:
            context = make_context(client)
            return await node(state, context), context

    return asyncio.run(scenario())


@pytest.fixture
def voice_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeVoiceNote:
    """build_voice_note's stand-in, writing into an output folder that doesn't exist
    yet."""
    fake = FakeVoiceNote()
    monkeypatch.setattr(delivery, "build_voice_note", fake)
    monkeypatch.setattr(delivery, "OUTPUT_DIR", tmp_path / "news_podcast_output")
    return fake


def test_narrate_builds_the_lessons_voice_and_reads_the_script_in_its_style(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    voice = FakeVoice(model=LESSON_SETTINGS["voice_model"])
    built: list[tuple[str, httpx.AsyncClient]] = []

    def provider(model: str, http_client: httpx.AsyncClient) -> VoiceProvider:
        built.append((model, http_client))
        return voice

    monkeypatch.setattr(delivery, "voice_provider", provider)

    update, context = run_node(
        narrate_lesson,
        {"turns": TURNS},
        lambda client: lesson_context(client, CATALOG, workdir=tmp_path),
    )

    style = LESSON_SETTINGS["voice_style"]
    assert built == [(LESSON_SETTINGS["voice_model"], context.http_client)]
    assert voice.chunks == [TURNS]
    assert voice.hosts == [HOSTS]
    assert voice.styles == [(style, style, "voice")]
    assert update == {"parts": [tmp_path / "voice_001.wav"]}
    assert context.progress.usages == [
        Usage(step="voice", model=LESSON_SETTINGS["voice_model"], output_tokens=1_000)
    ]


def test_narrate_waits_out_of_the_runs_own_waits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    voice = FakeVoice([VoiceUnavailableError("503")])
    monkeypatch.setattr(delivery, "voice_provider", lambda *_args: voice)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    waits = [3, 7]

    run_node(
        narrate_lesson,
        {"turns": TURNS},
        lambda client: lesson_context(
            client, CATALOG, workdir=tmp_path, voice_waits=waits
        ),
    )

    assert slept == [180]
    assert waits == [7]


def test_build_audio_writes_todays_lesson_at_the_speech_speed(
    voice_note: FakeVoiceNote, tmp_path: Path
) -> None:
    parts = [tmp_path / "voice_001.wav"]
    warnings: list[str] = []

    update, _ = run_node(
        build_audio,
        {"parts": parts},
        lambda client: lesson_context(
            client,
            CATALOG,
            options=build_lesson_options(speech_speed=1.25),
            workdir=tmp_path,
            warnings=warnings,
        ),
    )

    audio = tmp_path / "news_podcast_output" / f"aula-{TODAY:%Y-%m-%d}.ogg"
    assert voice_note.calls == [(parts, audio, 1.25, tmp_path)]
    assert update == {"audio": audio, "duration_seconds": 545.6}
    assert audio.read_bytes() == b"ogg"
    assert warnings == []


@pytest.mark.parametrize(("duration", "warned"), [(600.0, 0), (600.5, 1)])
def test_a_lesson_over_ten_minutes_still_goes_out_with_one_warning(
    voice_note: FakeVoiceNote, tmp_path: Path, duration: float, warned: int
) -> None:
    voice_note.duration = duration
    warnings: list[str] = []

    update, _ = run_node(
        build_audio,
        {"parts": [tmp_path / "voice_001.wav"]},
        lambda client: lesson_context(
            client, CATALOG, workdir=tmp_path, warnings=warnings
        ),
    )

    assert update["duration_seconds"] == duration
    assert len(warnings) == warned
    assert all("10:01 min" in warning for warning in warnings)


@pytest.mark.parametrize("missing", ["bot", "chat"])
def test_send_without_the_bot_or_the_chat_fails_the_lesson(missing: str) -> None:
    sent: list[tuple[object, ...]] = []

    def make_context(client: httpx.AsyncClient) -> LessonContext:
        return lesson_context(
            client,
            CATALOG,
            telegram=None if missing == "bot" else RecordingTelegram(client, sent),
            chat=None if missing == "chat" else "-22",
        )

    with pytest.raises(
        LessonError,
        match="TELEGRAM_BOT_TOKEN ou TELEGRAM_ADMIN_CHAT_ID não configurado",
    ):
        run_node(send_lesson, SENT_STATE, make_context)
    assert sent == []
