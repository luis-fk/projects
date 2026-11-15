import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    close_old_connections,
)
from pytest_django import Settings
from tavily import AsyncTavilyClient

from news_podcast.api.core import telegram
from news_podcast.api.core.config import (
    MAX_RUN_MINUTES,
    TEXT_RETRY_MINUTES,
    VOICE_RETRY_MINUTES,
)
from news_podcast.api.core.llm import ModelFactory, TextModels
from news_podcast.api.core.schemas import NewsStory, Usage
from news_podcast.api.core.telegram import TelegramClient, TelegramError
from news_podcast.api.core.voice.base import VoiceError
from news_podcast.api.core.voice.providers import voice_provider
from news_podcast.api.episode import run as episode_run
from news_podcast.api.episode.run import Selection
from news_podcast.api.episode.schemas import EpisodeReport, NewsItem, SelectedStory
from news_podcast.api.episode.service import EpisodeError, EpisodeService
from news_podcast.api.lesson import delivery, graph
from news_podcast.api.lesson import run as lesson_run
from news_podcast.api.lesson.config import MAX_LESSON_MINUTES
from news_podcast.api.lesson.graph import LessonOutcome, run_graph
from news_podcast.api.lesson.schemas import LessonPage, TopicToTry
from news_podcast.api.lesson.state import LessonContext, LessonState
from news_podcast.api.lesson.topics import candidates
from news_podcast.models import (
    Episode,
    Feed,
    PodcastSettings,
    SettingsMissing,
    Topic,
)
from tests.news_podcast.factories import (
    LESSON_SETTINGS,
    SETTINGS,
    build_lesson_options,
    build_lesson_settings,
    build_settings,
    catalog_topic,
    create_area,
    create_lesson_row,
    create_lesson_settings,
    create_settings,
    create_topic,
)
from tests.news_podcast.fake_models import FakeChatModel, fake_factory
from tests.news_podcast.fake_voice import FakeVoice
from tests.news_podcast.lesson.context import (
    MODEL,
    FakeSearch,
    FakeVoiceNote,
    RecordingTelegram,
    answer,
    found,
    script_answer,
    search_result,
    words_in,
)

# transaction=True: the run closes its connection before writing the history, and a
# deleted topic fails the write only at the commit.
DATABASE = pytest.mark.django_db(
    databases=["default", "news_podcast_db"], transaction=True
)
STORIES = [
    NewsStory("Câmara aprova a reforma", "Política", "o que muda para quem paga"),
    NewsStory("Congresso derruba veto do presidente", "Política", "como o veto caiu"),
]
PAGES = [
    search_result(),
    search_result(url="https://www.camara.leg.br/veto", title="Vetos"),
    search_result(url="https://portal.stf.jus.br/veto", title="O STF e o veto"),
]
REVIEWED = script_answer(*words_in(990), changes=["Cortei."])
# The text answers of a lesson on its first topic: the choice, the pick, the script
# and the review.
LESSON_ANSWERS = [
    answer(1, 2),
    json.dumps({"pages": [1, 2, 3], "terms": ""}),
    script_answer(),
    REVIEWED,
]
# Each fake answer: 100 input and 15 output tokens of the text model.
ANSWER_COST = (
    100 * SETTINGS["text_input_price"] + 15 * SETTINGS["text_output_price"]
) / 1e6


def pick(pages: list[int], terms: str = "") -> str:
    return json.dumps({"pages": pages, "terms": terms})


class World:
    """The lesson's outside world, faked: Tavily, Telegram, the voice and ffmpeg.
    Telegram records in `sent`, for any client the run or a report builds."""

    def __init__(self, tmp_path: Path) -> None:
        # Each run's Tavily client is a new FakeSearch with these responses.
        self.responses: list[Any] = []
        self.searches: list[FakeSearch] = []
        self.tavily_keys: list[str] = []
        self.sent: list[tuple[Any, ...]] = []
        self.voice_outcomes: list[Exception | None] = []
        self.voice_models: list[str] = []
        self.note = FakeVoiceNote()
        self.output = tmp_path / "news_podcast_output"

    def tavily(self, api_key: str) -> FakeSearch:
        self.tavily_keys.append(api_key)
        self.searches.append(FakeSearch(*self.responses))
        return self.searches[-1]

    def telegram(self, http_client: httpx.AsyncClient) -> TelegramClient:
        return RecordingTelegram(http_client, self.sent)

    def voice(self, model: str, http_client: httpx.AsyncClient) -> FakeVoice:
        self.voice_models.append(model)
        return FakeVoice(self.voice_outcomes, model=model)

    def messages(self) -> list[tuple[str, str]]:
        return [(chat, text) for chat, text, *voice in self.sent if not voice]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path) -> World:
    world = World(tmp_path)
    settings.TAVILY_API_KEY = "tavily-test-key"
    settings.TELEGRAM_CHAT_ID = "chat"
    settings.TELEGRAM_ADMIN_CHAT_ID = "admin"
    monkeypatch.setattr(lesson_run, "AsyncTavilyClient", world.tavily)
    monkeypatch.setattr(lesson_run, "telegram_client", world.telegram)
    # report_failure looks the client up in core.telegram.
    monkeypatch.setattr(telegram, "telegram_client", world.telegram)
    monkeypatch.setattr(delivery, "voice_provider", world.voice)
    monkeypatch.setattr(delivery, "build_voice_note", world.note)
    monkeypatch.setattr(delivery, "OUTPUT_DIR", world.output)
    return world


def create_catalog() -> dict[str, Topic]:
    """Two areas: the first offers the veto, the second the three Powers."""
    congress = create_area(name="O Congresso e as leis", position=1)
    powers = create_area(name="Os Poderes e o Estado", position=2)
    return {
        "veto": create_topic(
            congress, position=1, title="Como o Congresso derruba um veto"
        ),
        "law": create_topic(congress, position=2, title="Como uma lei nasce"),
        "powers": create_topic(powers, position=1, title="Os três Poderes"),
    }


def make(
    stories: list[NewsStory],
    answers: list[Any],
    *,
    send: bool = True,
    usages: list[Usage] | None = None,
    asked: list[str] | None = None,
) -> Exception | None:
    # A copy: the fake model takes its answers off the list it gets.
    factory = fake_factory({MODEL: list(answers)}, [] if asked is None else asked)
    return lesson_run.make_lesson(
        stories,
        send=send,
        news_sent=False,
        usages=usages or [],
        model_factory=factory,
    )


def test_the_lesson_options_take_the_speed_from_settings() -> None:
    options = lesson_run.lesson_options(
        build_settings(speech_speed=1.25), build_lesson_settings()
    )

    assert options == build_lesson_options(speech_speed=1.25)


@DATABASE
def test_a_sent_lesson_is_saved_with_its_topic_story_and_pages(world: World) -> None:
    create_settings()
    create_lesson_settings()
    catalog = create_catalog()
    world.responses.append(found(*PAGES))

    error = make(STORIES, LESSON_ANSWERS)

    assert error is None
    episode = Episode.objects.get()
    assert episode.kind == Episode.Kind.LESSON
    assert episode.status == Episode.Status.SENT
    assert episode.topic == catalog["veto"]
    assert episode.story_title == "Congresso derruba veto do presidente"
    assert episode.pages == [
        {"title": page["title"], "url": page["url"]} for page in PAGES
    ]
    assert (episode.duration_seconds, episode.script_words) == (545.6, 990)
    assert episode.error == ""
    assert [usage["step"] for usage in episode.usage] == [
        "choice",
        "pick",
        "script",
        "review",
        "voice",
        "voice",
    ]
    [(chat, audio, caption, duration)] = world.sent
    assert (chat, duration) == ("admin", 546)
    assert audio.parent == world.output
    assert caption.startswith("🎓 <b>Aula · Como o Congresso derruba um veto</b>")
    assert world.voice_models == [LESSON_SETTINGS["voice_model"]]
    assert world.tavily_keys == ["tavily-test-key"]
    assert [search.closed for search in world.searches] == [True]


@DATABASE
def test_a_sent_lesson_takes_its_topic_out_of_the_candidates(world: World) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))

    make(STORIES, LESSON_ANSWERS)

    assert [topic.title for topic in candidates(Topic.catalog())] == [
        "Como uma lei nasce",
        "Os três Poderes",
    ]


@DATABASE
@pytest.mark.parametrize(
    "voice_model", [LESSON_SETTINGS["voice_model"], SETTINGS["voice_model"]]
)
def test_the_cost_counts_text_at_settings_prices_and_voice_at_the_lessons(
    monkeypatch: pytest.MonkeyPatch, voice_model: str
) -> None:
    create_settings()
    create_lesson_settings(voice_model=voice_model)
    usages = [
        Usage(step="script", model=SETTINGS["text_model"], input_tokens=1_000_000),
        Usage(step="voice", model=voice_model, output_tokens=1_000_000),
    ]

    async def ran(inputs: LessonState, context: LessonContext) -> LessonOutcome:
        context.progress.usages.extend(usages)
        return LessonOutcome(inputs, None, "envio", [], context.progress.usages)

    monkeypatch.setattr(lesson_run, "run_graph", ran)

    lesson_run.make_lesson([], send=False, news_sent=False)

    episode = Episode.objects.get()
    # 0.75 for the text and 9.00 for the voice: the lesson's voice price wins even
    # when both rows name the same voice model, whose Settings price is 6.00.
    assert episode.cost_usd == pytest.approx(9.75)
    assert episode.usage == [usage.model_dump() for usage in usages]
    assert episode.status == Episode.Status.NOT_SENT


@DATABASE
def test_a_voice_model_the_voice_refuses_fails_the_lesson_at_the_voice_step(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    # Saved without the admin's check, which refuses it.
    create_lesson_settings(voice_model="openai:gpt-4o-mini-tts")
    catalog = create_catalog()
    world.responses.append(found(*PAGES))
    monkeypatch.setattr(delivery, "voice_provider", voice_provider)

    error = make(STORIES, LESSON_ANSWERS)

    assert error is not None
    episode = Episode.objects.get()
    assert episode.status == Episode.Status.FAILED
    assert episode.error == f"voz: {error}"
    assert "openai:gpt-4o-mini-tts não é um modelo de voz conhecido" in episode.error
    assert episode.cost_usd == pytest.approx(4 * ANSWER_COST)
    assert episode.topic == catalog["veto"]
    assert len(episode.pages) == 3
    [(chat, text)] = world.sent
    assert chat == "admin"
    assert text.startswith("⚠️ Não consegui gerar a aula de hoje.\n<b>Etapa:</b> voz\n")


@DATABASE
def test_without_sending_a_failure_only_reaches_the_log(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    world.voice_outcomes.append(VoiceError("a voz caiu"))

    error = make(STORIES, LESSON_ANSWERS, send=False)

    assert str(error) == "a voz caiu"
    assert world.sent == []
    assert "The lesson failed at voz" in caplog.text
    episode = Episode.objects.get()
    assert (episode.status, episode.error) == (Episode.Status.FAILED, "voz: a voz caiu")


@DATABASE
def test_a_lesson_over_ten_minutes_still_goes_out_after_its_warning(
    world: World,
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    world.note.duration = 630.0

    make(STORIES, LESSON_ANSWERS)

    [warning, voice] = world.sent
    assert warning[0] == "admin"
    assert "10:30 min" in warning[1]
    assert (voice[0], voice[3]) == ("admin", 630)
    assert Episode.objects.get().status == Episode.Status.SENT


@DATABASE
def test_without_an_admin_chat_the_lesson_goes_to_the_episode_chat(
    world: World, settings: Settings
) -> None:
    settings.TELEGRAM_ADMIN_CHAT_ID = None
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))

    error = make(STORIES, LESSON_ANSWERS)

    assert error is None
    assert [entry[0] for entry in world.sent] == ["chat"]


class RefusingMessages(RecordingTelegram):
    async def send_message(self, chat_id: str, text: str) -> None:
        raise TelegramError("bot bloqueado no chat do admin")


@DATABASE
def test_a_refused_warning_is_logged_and_the_lesson_still_goes_out(
    monkeypatch: pytest.MonkeyPatch, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    world.note.duration = 630.0
    monkeypatch.setattr(
        lesson_run,
        "telegram_client",
        lambda client: RefusingMessages(client, world.sent),
    )

    error = make(STORIES, LESSON_ANSWERS)

    assert error is None
    assert "bot bloqueado no chat do admin" in caplog.text
    assert [entry[0] for entry in world.sent] == ["admin"]
    assert Episode.objects.get().status == Episode.Status.SENT


@DATABASE
@pytest.mark.parametrize("send", [True, False])
def test_the_time_limit_fails_the_lesson_at_the_step_running(
    monkeypatch: pytest.MonkeyPatch, world: World, send: bool
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()

    async def never(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(TextModels, "ask", never)
    monkeypatch.setattr(graph, "MAX_LESSON_MINUTES", 0.001)

    error = make(STORIES, [], send=send)

    assert str(error) == "A aula passou de 0.001 minutos"
    episode = Episode.objects.get()
    assert episode.status == Episode.Status.FAILED
    assert episode.error == "escolha do tema: A aula passou de 0.001 minutos"
    assert [chat for chat, _ in world.messages()] == (["admin"] if send else [])


def capture_run(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[LessonContext, bool]]:
    """Each run's context, with whether its client was open while the graph ran."""
    seen: list[tuple[LessonContext, bool]] = []

    async def ran(inputs: LessonState, context: LessonContext) -> LessonOutcome:
        seen.append((context, not context.http_client.is_closed))
        return LessonOutcome(inputs, None, "envio", [], context.progress.usages)

    monkeypatch.setattr(lesson_run, "run_graph", ran)
    return seen


@DATABASE
def test_each_lesson_builds_its_own_clients_inside_its_run(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    seen = capture_run(monkeypatch)
    selection = [Usage(step="selection", model=MODEL, input_tokens=10)]

    for _ in range(2):
        lesson_run.make_lesson([], send=False, news_sent=False, usages=selection)

    (first, open_a), (second, open_b) = seen
    assert open_a and open_b
    assert first.http_client is not second.http_client
    assert first.http_client.is_closed and second.http_client.is_closed
    assert first.text is not second.text
    assert first.text.http_client is first.http_client
    assert first.text.usages is first.progress.usages
    assert first.text.names == [SETTINGS["text_model"], SETTINGS["fallback_model"]]
    assert first.progress.usages == selection
    assert first.progress.usages is not selection
    assert first.voice_waits == list(VOICE_RETRY_MINUTES)
    assert first.voice_waits is not second.voice_waits
    assert (first.search, second.search) == tuple(world.searches)
    assert first.search is not second.search
    assert world.tavily_keys == ["tavily-test-key"] * 2
    assert [search.closed for search in world.searches] == [True, True]
    assert first.workdir != second.workdir
    assert not first.workdir.exists()


@DATABASE
def test_with_no_tavily_key_the_lesson_has_no_search_client(
    monkeypatch: pytest.MonkeyPatch, world: World, settings: Settings
) -> None:
    settings.TAVILY_API_KEY = None
    create_settings()
    create_lesson_settings()
    seen = capture_run(monkeypatch)

    lesson_run.make_lesson([], send=False, news_sent=False)

    [(context, _)] = seen
    assert context.search is None
    assert world.tavily_keys == []


def deleted_topic_outcome(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None, topic_id: int = 987_654
) -> None:
    """A run whose topic, by default, was deleted in the admin before the row is
    written."""
    gone = TopicToTry(
        catalog_topic(id=topic_id, title="Como o Congresso derruba um veto"), None
    )
    page = LessonPage(
        "Vetos", "www.camara.leg.br", "https://camara.leg.br/v", None, "", ""
    )

    async def ran(inputs: LessonState, context: LessonContext) -> LessonOutcome:
        state: LessonState = {**inputs, "topic": gone, "pages": [page], "words": 990}
        return LessonOutcome(state, error, "envio", [], context.progress.usages)

    monkeypatch.setattr(lesson_run, "run_graph", ran)


@DATABASE
@pytest.mark.parametrize(
    ("error", "status", "saved_error"),
    [
        (None, Episode.Status.SENT, "tema apagado: Como o Congresso derruba um veto"),
        (
            TelegramError("recusado"),
            Episode.Status.FAILED,
            "envio: recusado (tema apagado: Como o Congresso derruba um veto)",
        ),
    ],
)
def test_a_topic_deleted_during_the_run_is_left_out_of_the_row(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    error: Exception | None,
    status: str,
    saved_error: str,
) -> None:
    create_settings()
    create_lesson_settings()
    deleted_topic_outcome(monkeypatch, error)

    lesson_run.make_lesson([], send=True, news_sent=False)

    episode = Episode.objects.get()
    assert episode.topic is None
    assert (episode.status, episode.error) == (status, saved_error)
    assert episode.pages == [{"title": "Vetos", "url": "https://camara.leg.br/v"}]
    warnings = [text for chat, text in world.messages() if "foi apagado" in text]
    assert len(warnings) == 1
    assert "<b>Como o Congresso derruba um veto</b>" in warnings[0]


@DATABASE
def test_a_deleted_topic_without_sending_is_only_logged(
    monkeypatch: pytest.MonkeyPatch, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    create_settings()
    create_lesson_settings()
    deleted_topic_outcome(monkeypatch, None)

    lesson_run.make_lesson([], send=False, news_sent=False)

    assert Episode.objects.get().topic is None
    assert world.sent == []
    assert "foi apagado" in caplog.text


@DATABASE
def test_a_row_that_fails_twice_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    deleted_topic_outcome(monkeypatch, None)
    errors = [IntegrityError("o tema sumiu"), DatabaseError("o banco caiu")]
    written: list[dict[str, Any]] = []

    def broken(**values: Any) -> Episode:
        written.append(values)
        raise errors.pop(0)

    monkeypatch.setattr(Episode.objects, "create", broken)

    with pytest.raises(DatabaseError, match="o banco caiu"):
        lesson_run.make_lesson([], send=False, news_sent=False)
    assert [values["topic_id"] for values in written] == [987_654, None]


@DATABASE
@pytest.mark.parametrize(
    ("failure", "topic_kept"),
    [
        (OperationalError("a conexão caiu"), True),
        (IntegrityError("outra regra"), True),
        (OperationalError("a conexão caiu"), False),
    ],
)
def test_a_failed_write_that_is_not_a_deleted_topic_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    failure: Exception,
    topic_kept: bool,
) -> None:
    create_settings()
    create_lesson_settings()
    topic = create_catalog()["veto"]
    deleted_topic_outcome(monkeypatch, None, topic_id=topic.pk)
    if not topic_kept:
        topic.delete()
    written: list[dict[str, Any]] = []

    def broken(**values: Any) -> Episode:
        written.append(values)
        raise failure

    monkeypatch.setattr(Episode.objects, "create", broken)

    with pytest.raises(type(failure)):
        lesson_run.make_lesson([], send=True, news_sent=False)
    assert len(written) == 1
    assert world.sent == []


class LateEvening:
    """23:30 in São Paulo on 2026-03-09, already 02:30 on the 10th in UTC: a day
    that is not today, so a clock that skips this one can't pass."""

    @staticmethod
    def now(tz: tzinfo) -> datetime:
        return datetime(2026, 3, 10, 2, 30, tzinfo=UTC).astimezone(tz)


@DATABASE
def test_a_late_evening_lesson_takes_its_sao_paulo_date(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    seen = capture_run(monkeypatch)
    monkeypatch.setattr(lesson_run, "datetime", LateEvening)

    lesson_run.make_lesson([], send=False, news_sent=False)

    [(context, _)] = seen
    assert context.today == date(2026, 3, 9)


@DATABASE
@pytest.mark.parametrize("send", [True, False])
def test_a_run_whose_clients_fail_is_still_saved_and_reported(
    monkeypatch: pytest.MonkeyPatch, world: World, send: bool
) -> None:
    create_settings()
    create_lesson_settings()

    def broken(api_key: str) -> FakeSearch:
        raise RuntimeError("o cliente do Tavily não abriu")

    monkeypatch.setattr(lesson_run, "AsyncTavilyClient", broken)
    selection = [Usage(step="selection", model=MODEL, input_tokens=1_000_000)]

    error = lesson_run.make_lesson([], send=send, news_sent=False, usages=selection)

    assert str(error) == "o cliente do Tavily não abriu"
    episode = Episode.objects.get()
    assert episode.status == Episode.Status.FAILED
    assert episode.error == "início: o cliente do Tavily não abriu"
    assert episode.cost_usd == pytest.approx(SETTINGS["text_input_price"])
    notices = [chat for chat, text in world.messages() if "Etapa:</b> início" in text]
    assert notices == (["admin"] if send else [])


def test_the_search_client_is_tavilys_own_and_opens_and_closes(
    settings: Settings,
) -> None:
    settings.TAVILY_API_KEY = "tavily-test-key"

    async def scenario() -> object:
        async with lesson_run.search_client() as client:
            return client

    assert isinstance(asyncio.run(scenario()), AsyncTavilyClient)


def lesson_over(
    monkeypatch: pytest.MonkeyPatch, factory_or_answers: ModelFactory | list[Any]
) -> None:
    """The action's lesson asks the fake text model, given its answers or its
    factory."""
    make_lesson = lesson_run.make_lesson
    factory = (
        fake_factory({MODEL: list(factory_or_answers)}, [])
        if isinstance(factory_or_answers, list)
        else factory_or_answers
    )

    def faked(stories: list[NewsStory], **options: Any) -> Exception | None:
        return make_lesson(stories, **options, model_factory=factory)

    monkeypatch.setattr(lesson_run, "make_lesson", faked)


def selection_of(monkeypatch: pytest.MonkeyPatch, selection: Selection) -> list[bool]:
    calls: list[bool] = []

    def select_today() -> Selection:
        calls.append(True)
        return selection

    monkeypatch.setattr(episode_run, "select_today", select_today)
    return calls


SELECTION_USAGE = Usage(step="selection", model=MODEL, input_tokens=1_000_000)


@DATABASE
def test_the_lesson_action_counts_the_selection_before_the_lesson(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    selection_of(monkeypatch, Selection(STORIES, [SELECTION_USAGE], None))
    lesson_over(monkeypatch, LESSON_ANSWERS)

    call_command("news_podcast", "lesson")

    episode = Episode.objects.get()
    assert [usage["step"] for usage in episode.usage][:2] == ["selection", "choice"]
    voice = 2 * 1_000 * LESSON_SETTINGS["voice_output_price"] / 1e6
    selection = SETTINGS["text_input_price"]
    assert episode.cost_usd == pytest.approx(selection + 4 * ANSWER_COST + voice)


@DATABASE
def test_after_a_failed_selection_the_lesson_takes_the_course_order(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    catalog = create_catalog()
    world.responses.append(found(*PAGES))
    failed = Selection([], [SELECTION_USAGE], EpisodeError("a seleção falhou"))
    selection_of(monkeypatch, failed)
    # No stories, so no choice is asked.
    lesson_over(monkeypatch, LESSON_ANSWERS[1:])

    call_command("news_podcast", "lesson")

    episode = Episode.objects.get()
    assert episode.topic == catalog["veto"]
    assert episode.story_title == ""
    assert [usage["step"] for usage in episode.usage][:2] == ["selection", "pick"]


@DATABASE
def test_without_the_catalog_the_command_fails_at_the_topic_step(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    selection_of(monkeypatch, Selection(STORIES, [SELECTION_USAGE], None))
    lesson_over(monkeypatch, [])

    with pytest.raises(CommandError, match="O catálogo de temas está vazio"):
        call_command("news_podcast", "lesson")

    episode = Episode.objects.get()
    assert episode.status == Episode.Status.FAILED
    assert episode.error.startswith("escolha do tema: O catálogo de temas está vazio")
    assert episode.topic is None
    assert episode.cost_usd == pytest.approx(SETTINGS["text_input_price"])
    [(chat, text)] = world.sent
    assert chat == "admin"
    assert "<b>Etapa:</b> escolha do tema" in text


@DATABASE
def test_two_topics_short_of_pages_fail_the_command_at_the_search_step(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.extend([found(PAGES[0])] * 2)
    selection_of(monkeypatch, Selection(STORIES, [], None))
    lesson_over(monkeypatch, [answer(1, 2), pick([1]), pick([1])])

    with pytest.raises(CommandError, match="Nenhum tema teve pelo menos 2 páginas"):
        call_command("news_podcast", "lesson")

    episode = Episode.objects.get()
    assert episode.error == (
        "busca das fontes: Nenhum tema teve pelo menos 2 páginas boas: "
        "Como o Congresso derruba um veto; Os três Poderes."
    )
    assert episode.topic is None
    assert episode.pages == []
    assert episode.cost_usd == pytest.approx(3 * ANSWER_COST)
    [(chat, text)] = world.sent
    assert chat == "admin"
    assert "Não consegui gerar a aula de hoje" in text


@DATABASE
@pytest.mark.parametrize("failure", ["no lesson settings", "catalog"])
def test_a_settings_failure_is_reported_with_no_row_and_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, world: World, failure: str
) -> None:
    create_settings()
    if failure == "catalog":
        create_lesson_settings()

        def broken() -> list[Any]:
            raise DatabaseError("a conexão caiu")

        monkeypatch.setattr(Topic, "catalog", broken)
    selection_of(monkeypatch, Selection(STORIES, [], None))
    lesson_over(monkeypatch, [])

    with pytest.raises(CommandError):
        call_command("news_podcast", "lesson")

    assert not Episode.objects.exists()
    [(chat, text)] = world.sent
    assert chat == "admin"
    assert "Não consegui gerar a aula de hoje" in text
    assert "<b>Etapa:</b> configurações" in text


@DATABASE
def test_without_podcast_settings_the_action_sends_one_notice_and_fails(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_lesson_settings()
    lesson_over(monkeypatch, [])

    with pytest.raises(CommandError, match="seed_news_podcast"):
        call_command("news_podcast", "lesson")

    assert not Episode.objects.exists()
    [(chat, text)] = world.sent
    assert chat == "admin"
    assert "Não consegui gerar a aula de hoje" in text
    assert "<b>Etapa:</b> configurações" in text


SELECTED = json.dumps(
    {
        "stories": [
            {
                "title": story.title,
                "category": "politica",
                "minutes": 2,
                "angle": story.angle,
                "item_ids": [str(number)],
            }
            for number, story in enumerate(STORIES, start=1)
        ]
    }
)


@DATABASE
def test_the_lesson_action_without_sending_makes_the_whole_lesson_locally(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    Feed.objects.create(
        outlet="g1", section=Feed.Section.POLITICA, url="https://g1.globo.com/rss/g1/"
    )
    world.responses.append(found(*PAGES))

    async def fetch(*_args: object) -> list[NewsItem]:
        return [
            NewsItem(
                id=str(number),
                outlet="g1",
                section="Política",
                title=story.title,
                summary="",
                link="",
                published_at=None,
            )
            for number, story in enumerate(STORIES, start=1)
        ]

    asked: list[str] = []
    built: dict[str, FakeChatModel] = {}
    factory = fake_factory({MODEL: [SELECTED, *LESSON_ANSWERS]}, asked, built)
    select_today = episode_run.select_today
    monkeypatch.setattr(episode_run, "fetch_items", fetch)
    monkeypatch.setattr(
        episode_run, "select_today", lambda: select_today(model_factory=factory)
    )
    lesson_over(monkeypatch, factory)
    assert not world.output.exists()

    call_command("news_podcast", "lesson", "--no-send")

    # The selection, then the choice, the pick, the script and the review.
    assert asked == [MODEL] * 5
    script_request = built[MODEL].received[2]
    assert "Resumo enviado: não." in str(script_request[0].content)
    assert world.voice_models == [LESSON_SETTINGS["voice_model"]]
    [(_, audio, speed, _)] = world.note.calls
    assert audio.parent == world.output
    assert audio.name.startswith("aula-") and audio.suffix == ".ogg"
    assert audio.read_bytes() == b"ogg"
    assert speed == SETTINGS["speech_speed"]
    assert world.sent == []
    episode = Episode.objects.get()
    assert (episode.kind, episode.status) == (
        Episode.Kind.LESSON,
        Episode.Status.NOT_SENT,
    )
    assert [usage["step"] for usage in episode.usage] == [
        "selection",
        "choice",
        "pick",
        "script",
        "review",
        "voice",
        "voice",
    ]


@DATABASE
def test_without_sending_a_settings_failure_only_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    selection_of(monkeypatch, Selection(STORIES, [], None))
    lesson_over(monkeypatch, [])

    with pytest.raises(CommandError, match="seed_news_podcast"):
        call_command("news_podcast", "lesson", "--no-send")

    assert world.sent == []
    assert not Episode.objects.exists()


@asynccontextmanager
async def closing_fails() -> AsyncIterator[None]:
    yield None
    raise RuntimeError("o cliente do Tavily não fechou")


@DATABASE
def test_a_client_that_fails_to_close_after_the_lesson_keeps_its_outcome(
    monkeypatch: pytest.MonkeyPatch, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    create_settings()
    create_lesson_settings()
    capture_run(monkeypatch)
    monkeypatch.setattr(lesson_run, "search_client", closing_fails)

    error = lesson_run.make_lesson([], send=True, news_sent=False)

    assert error is None
    assert Episode.objects.get().status == Episode.Status.SENT
    assert world.sent == []
    assert "o cliente do Tavily não fechou" in caplog.text


def clock(instant: datetime) -> type:
    """A stand-in for run.py's datetime whose now() is `instant`."""

    class Clock:
        @staticmethod
        def now(tz: tzinfo) -> datetime:
            return instant.astimezone(tz)

    return Clock


# D is 2026-03-09 in São Paulo; its lesson went out at 23:30, 02:30 UTC on the 10th.
D_EVENING = datetime(2026, 3, 10, 2, 30, tzinfo=UTC)
# The cron's 06:40 in São Paulo on D+1 and D+2.
D1_MORNING = datetime(2026, 3, 10, 9, 40, tzinfo=UTC)
D2_MORNING = datetime(2026, 3, 11, 9, 40, tzinfo=UTC)


@pytest.mark.parametrize(
    ("last_sent_on", "every_days", "due"),
    [
        (None, 2, True),
        (date(2026, 3, 9), 2, False),
        (date(2026, 3, 8), 2, True),
        (date(2026, 3, 7), 2, True),
        (date(2026, 3, 10), 1, False),
        (date(2026, 3, 9), 1, True),
    ],
)
def test_a_lesson_is_due_every_n_days_after_the_last_one_sent(
    last_sent_on: date | None, every_days: int, due: bool
) -> None:
    assert lesson_run.lesson_due(date(2026, 3, 10), last_sent_on, every_days) is due


@DATABASE
def test_the_last_lesson_sent_is_read_on_its_sao_paulo_date() -> None:
    assert lesson_run.last_lesson_sent_on() is None

    create_lesson_row(started_at=D_EVENING)
    later = datetime(2026, 3, 12, 9, 40, tzinfo=UTC)
    create_lesson_row(started_at=later, status=Episode.Status.FAILED)
    create_lesson_row(started_at=later, status=Episode.Status.NOT_SENT)
    create_lesson_row(started_at=later, kind=Episode.Kind.NEWS)

    assert lesson_run.last_lesson_sent_on() == date(2026, 3, 9)


@DATABASE
def test_with_a_lesson_every_two_days_only_the_second_day_makes_one(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    create_lesson_row(started_at=D_EVENING)
    seen = capture_run(monkeypatch)

    def run_at(instant: datetime) -> Exception | None:
        monkeypatch.setattr(lesson_run, "datetime", clock(instant))
        return lesson_run.make_lesson([], send=False, news_sent=True, scheduled=True)

    assert run_at(D1_MORNING) is None
    assert seen == []
    assert run_at(D2_MORNING) is None
    assert len(seen) == 1
    # Once D+2's lesson went out, a second run that day makes none.
    create_lesson_row(started_at=D2_MORNING)
    run_at(D2_MORNING + timedelta(hours=1))
    assert len(seen) == 1
    assert Episode.objects.filter(status=Episode.Status.NOT_SENT).count() == 1


@DATABASE
def test_a_failed_or_unsent_lesson_leaves_the_next_one_due(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    yesterday = D2_MORNING - timedelta(days=1)
    create_lesson_row(started_at=yesterday, status=Episode.Status.FAILED)
    create_lesson_row(started_at=yesterday, status=Episode.Status.NOT_SENT)
    seen = capture_run(monkeypatch)
    monkeypatch.setattr(lesson_run, "datetime", clock(D2_MORNING))

    lesson_run.make_lesson([], send=False, news_sent=True, scheduled=True)

    assert len(seen) == 1


@DATABASE
@pytest.mark.parametrize(("scheduled", "runs"), [(True, False), (False, True)])
def test_paused_skips_only_a_scheduled_lesson(
    monkeypatch: pytest.MonkeyPatch, world: World, scheduled: bool, runs: bool
) -> None:
    create_settings(paused=True)
    create_lesson_settings()
    seen = capture_run(monkeypatch)

    error = lesson_run.make_lesson([], send=True, news_sent=True, scheduled=scheduled)

    assert error is None
    assert len(seen) == int(runs)
    assert Episode.objects.count() == int(runs)


@DATABASE
def test_a_failed_read_of_the_last_lesson_is_reported_with_no_row(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    create_lesson_settings()
    seen = capture_run(monkeypatch)

    def broken() -> date | None:
        raise DatabaseError("a conexão caiu")

    monkeypatch.setattr(lesson_run, "last_lesson_sent_on", broken)

    error = lesson_run.make_lesson([], send=True, news_sent=True, scheduled=True)

    assert str(error) == "a conexão caiu"
    assert seen == []
    assert not Episode.objects.exists()
    [(chat, text)] = world.messages()
    assert chat == "admin"
    assert "<b>Etapa:</b> configurações" in text


@DATABASE
def test_without_a_cadence_the_cron_makes_no_lesson_and_says_why(
    monkeypatch: pytest.MonkeyPatch, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    create_settings()
    create_lesson_settings(every_days=None)
    seen = capture_run(monkeypatch)

    with caplog.at_level(logging.INFO, logger=lesson_run.__name__):
        error = lesson_run.make_lesson([], send=True, news_sent=True, scheduled=True)

    assert error is None
    assert seen == []
    assert not Episode.objects.exists()
    assert world.sent == []
    assert "Lesson settings has no cadence" in caplog.text


@DATABASE
@pytest.mark.parametrize("every_days", [None, 2])
def test_a_lesson_on_demand_ignores_the_cadence(
    monkeypatch: pytest.MonkeyPatch, world: World, every_days: int | None
) -> None:
    create_settings()
    create_lesson_settings(every_days=every_days)
    # A lesson already went out today.
    create_lesson_row(started_at=D2_MORNING)
    seen = capture_run(monkeypatch)
    monkeypatch.setattr(lesson_run, "datetime", clock(D2_MORNING))

    lesson_run.make_lesson([], send=False, news_sent=False)

    assert len(seen) == 1


@DATABASE
def test_the_cron_reads_its_cadence_from_lesson_settings_and_reports_a_missing_row(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings()
    # A lesson already went out today, but the cadence lives in the missing row.
    create_lesson_row(started_at=D2_MORNING)
    seen = capture_run(monkeypatch)
    monkeypatch.setattr(lesson_run, "datetime", clock(D2_MORNING))

    error = lesson_run.make_lesson([], send=True, news_sent=True, scheduled=True)

    assert isinstance(error, SettingsMissing)
    assert seen == []
    assert Episode.objects.count() == 1
    [(chat, text)] = world.messages()
    assert chat == "admin"
    assert "<b>Etapa:</b> configurações" in text


@DATABASE
def test_a_paused_cron_run_reads_no_lesson_settings(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    create_settings(paused=True)
    seen = capture_run(monkeypatch)

    error = lesson_run.make_lesson([], send=True, news_sent=True, scheduled=True)

    assert error is None
    assert seen == []
    assert world.sent == []


SELECTED_STORIES = [
    SelectedStory(
        title=story.title,
        category="politica",
        angle=story.angle,
        item_ids=[str(number)],
    )
    for number, story in enumerate(STORIES, start=1)
]


def episode_ends(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    tmp_path: Path,
    failure: Exception | None = None,
    selected: bool = True,
    seen: list[TextModels] | None = None,
) -> None:
    """The news episode, its steps faked inside its real run: the selection's
    stories, unless it fails before them, then its voice note or its failure. It
    sends through the world's Telegram, and `seen` gets its TextModels."""

    async def generate(service: EpisodeService, *_when: object) -> EpisodeReport:
        if seen is not None:
            seen.append(service.text)
        if selected:
            service.stories = SELECTED_STORIES
        if failure:
            raise failure
        return EpisodeReport(
            audio_path=tmp_path / "resumo.ogg",
            duration_seconds=900.0,
            caption="🎙️ Resumo das 7",
            script_words=2550,
        )

    monkeypatch.setattr(EpisodeService, "_generate", generate)
    monkeypatch.setattr(episode_run, "telegram_client", world.telegram)


def kinds_and_statuses() -> list[tuple[str, str]]:
    return [(row.kind, row.status) for row in Episode.objects.order_by("pk")]


NEWS, LESSON = Episode.Kind.NEWS, Episode.Kind.LESSON


@DATABASE
def test_with_a_blank_cadence_the_cron_sends_only_the_news(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    create_lesson_settings(every_days=None)
    create_catalog()
    episode_ends(monkeypatch, world, tmp_path)
    lesson_over(monkeypatch, [])

    call_command("news_podcast", "episode", "--scheduled")

    assert kinds_and_statuses() == [(NEWS, Episode.Status.SENT)]
    assert len(world.sent) == 1


@DATABASE
def test_a_run_by_hand_makes_no_lesson_even_when_one_is_due(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    episode_ends(monkeypatch, world, tmp_path)
    lesson_over(monkeypatch, [])

    call_command("news_podcast", "episode")

    assert kinds_and_statuses() == [(NEWS, Episode.Status.SENT)]
    assert len(world.sent) == 1


@DATABASE
def test_a_due_lesson_follows_the_sent_news_and_saves_its_row_after(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    episode_ends(monkeypatch, world, tmp_path)
    built: dict[str, FakeChatModel] = {}
    lesson_over(monkeypatch, fake_factory({MODEL: list(LESSON_ANSWERS)}, [], built))

    call_command("news_podcast", "episode", "--scheduled")

    sent = Episode.Status.SENT
    assert kinds_and_statuses() == [(NEWS, sent), (LESSON, sent)]
    assert [entry[2] for entry in world.sent][0] == "🎙️ Resumo das 7"
    assert world.sent[1][2].startswith("🎓 <b>Aula · ")
    # The news goes to the group, the lesson to the owner's chat.
    assert [entry[0] for entry in world.sent] == ["chat", "admin"]
    script_request = built[MODEL].received[2]
    assert "Resumo enviado: sim." in str(script_request[0].content)


@DATABASE
def test_without_sending_both_rows_are_not_sent_and_nothing_goes_out(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    episode_ends(monkeypatch, world, tmp_path)
    lesson_over(monkeypatch, LESSON_ANSWERS)

    call_command("news_podcast", "episode", "--scheduled", "--no-send")

    not_sent = Episode.Status.NOT_SENT
    assert kinds_and_statuses() == [(NEWS, not_sent), (LESSON, not_sent)]
    assert world.sent == []


@DATABASE
def test_both_failures_are_saved_reported_once_each_and_named(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    world.voice_outcomes.append(VoiceError("a voz da aula caiu"))
    episode_ends(monkeypatch, world, tmp_path, EpisodeError("a voz caiu"))
    built: dict[str, FakeChatModel] = {}
    lesson_over(monkeypatch, fake_factory({MODEL: list(LESSON_ANSWERS)}, [], built))

    with pytest.raises(CommandError) as raised:
        call_command("news_podcast", "episode", "--scheduled")

    assert str(raised.value) == "episódio: a voz caiu; aula: a voz da aula caiu"
    failed = Episode.Status.FAILED
    assert kinds_and_statuses() == [(NEWS, failed), (LESSON, failed)]
    news = Episode.objects.get(kind=NEWS)
    assert news.error.endswith(": a voz caiu")
    notices = [text.split("\n")[0] for _, text in world.messages()]
    assert notices == [
        "⚠️ Não consegui gerar o episódio de hoje.",
        "⚠️ Não consegui gerar a aula de hoje.",
    ]
    # The stories the failed episode selected, and no news sent.
    choice_request = str(built[MODEL].received[0][1].content)
    assert all(story.title in choice_request for story in STORIES)
    script_request = built[MODEL].received[2]
    assert "Resumo enviado: não." in str(script_request[0].content)


@DATABASE
def test_an_episode_that_fails_before_its_selection_leaves_the_course_order(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    create_lesson_settings()
    catalog = create_catalog()
    world.responses.append(found(*PAGES))
    failure = EpisodeError("Nenhuma notícia encontrada nos feeds")
    episode_ends(monkeypatch, world, tmp_path, failure, selected=False)
    # No stories, so no choice is asked.
    lesson_over(monkeypatch, LESSON_ANSWERS[1:])

    with pytest.raises(CommandError, match="^episódio: Nenhuma notícia"):
        call_command("news_podcast", "episode", "--scheduled")

    assert kinds_and_statuses() == [
        (NEWS, Episode.Status.FAILED),
        (LESSON, Episode.Status.SENT),
    ]
    lesson = Episode.objects.get(kind=LESSON)
    assert lesson.topic == catalog["veto"]
    assert lesson.story_title == ""
    notices = [text.split("\n")[0] for _, text in world.messages()]
    assert notices == ["⚠️ Não consegui gerar o episódio de hoje."]


@DATABASE
def test_a_failed_episode_and_no_lesson_settings_name_both_parts(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    episode_ends(monkeypatch, world, tmp_path, EpisodeError("a voz caiu"))
    lesson_over(monkeypatch, [])

    with pytest.raises(CommandError) as raised:
        call_command("news_podcast", "episode", "--scheduled")

    assert str(raised.value).startswith(
        "episódio: a voz caiu; aula: Não há configurações da aula no banco"
    )
    assert kinds_and_statuses() == [(NEWS, Episode.Status.FAILED)]
    notices = [text for _, text in world.messages()]
    assert len(notices) == 2
    assert "Não consegui gerar a aula de hoje" in notices[1]
    assert "<b>Etapa:</b> configurações" in notices[1]


@DATABASE
def test_paused_while_the_news_runs_stops_the_lesson(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    row = create_settings()
    create_lesson_settings()
    create_catalog()
    episode_ends(monkeypatch, world, tmp_path)
    lesson_over(monkeypatch, LESSON_ANSWERS)

    def tick_paused() -> None:
        # The owner ticks paused in the admin while the episode runs.
        close_old_connections()
        PodcastSettings.objects.filter(pk=row.pk).update(paused=True)

    monkeypatch.setattr(episode_run, "close_old_connections", tick_paused)

    call_command("news_podcast", "episode", "--scheduled")

    assert kinds_and_statuses() == [(NEWS, Episode.Status.SENT)]
    assert len(world.sent) == 1


@DATABASE
def test_paused_at_the_start_runs_neither_part(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings(paused=True)
    create_lesson_settings()
    episode_ends(monkeypatch, world, tmp_path)
    lesson_over(monkeypatch, [])

    call_command("news_podcast", "episode", "--scheduled")

    assert not Episode.objects.exists()
    assert world.sent == []


@DATABASE
def test_a_news_settings_failure_is_reported_once_with_no_lesson(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_lesson_settings()
    episode_ends(monkeypatch, world, tmp_path)
    lesson_over(monkeypatch, [])

    with pytest.raises(CommandError, match="seed_news_podcast"):
        call_command("news_podcast", "episode", "--scheduled")

    assert not Episode.objects.exists()
    [(chat, text)] = world.messages()
    assert chat == "admin"
    assert "Não consegui gerar o episódio de hoje" in text


@DATABASE
def test_the_lesson_starts_with_fresh_text_models_and_its_own_limit(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    episode_texts: list[TextModels] = []
    episode_ends(monkeypatch, world, tmp_path, seen=episode_texts)
    lesson_over(monkeypatch, LESSON_ANSWERS)
    lesson_texts: list[TextModels] = []

    async def spent_then_run(
        inputs: LessonState, context: LessonContext
    ) -> LessonOutcome:
        lesson_texts.append(context.text)
        return await run_graph(inputs, context)

    def spent(texts: list[TextModels]) -> None:
        # The episode used its waits and marked its model out of quota.
        texts[0].retry_minutes.clear()
        texts[0].dropped.add(SETTINGS["text_model"])

    monkeypatch.setattr(lesson_run, "run_graph", spent_then_run)
    limits: list[float] = []
    timeout = asyncio.timeout

    def recorded(delay: float | None) -> asyncio.Timeout:
        if delay is not None:
            limits.append(delay)
        return timeout(delay)

    monkeypatch.setattr(asyncio, "timeout", recorded)

    def after_the_episode() -> None:
        spent(episode_texts)
        close_old_connections()

    monkeypatch.setattr(episode_run, "close_old_connections", after_the_episode)

    call_command("news_podcast", "episode", "--scheduled")

    [lesson_text] = lesson_texts
    assert lesson_text is not episode_texts[0]
    assert lesson_text.retry_minutes == list(TEXT_RETRY_MINUTES)
    assert lesson_text.dropped == set()
    assert limits == [MAX_RUN_MINUTES * 60, MAX_LESSON_MINUTES * 60]
    assert Episode.objects.get(kind=LESSON).status == Episode.Status.SENT


@DATABASE
def test_a_failed_lesson_leaves_the_sent_news_as_it_went(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    create_settings()
    create_lesson_settings()
    create_catalog()
    world.responses.append(found(*PAGES))
    world.voice_outcomes.append(VoiceError("a voz da aula caiu"))
    episode_ends(monkeypatch, world, tmp_path)
    lesson_over(monkeypatch, LESSON_ANSWERS)

    with pytest.raises(CommandError) as raised:
        call_command("news_podcast", "episode", "--scheduled")

    assert str(raised.value) == "aula: a voz da aula caiu"
    assert kinds_and_statuses() == [
        (NEWS, Episode.Status.SENT),
        (LESSON, Episode.Status.FAILED),
    ]
    assert Episode.objects.get(kind=NEWS).error == ""
    assert world.sent[0][2] == "🎙️ Resumo das 7"
    notices = [text.split("\n")[0] for _, text in world.messages()]
    assert notices == ["⚠️ Não consegui gerar a aula de hoje."]
