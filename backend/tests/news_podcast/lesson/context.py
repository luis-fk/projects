import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from news_podcast.api.core.config import VOICE_RETRY_MINUTES
from news_podcast.api.core.llm import TextModels
from news_podcast.api.core.schemas import NewsStory, Usage
from news_podcast.api.core.telegram import TelegramClient
from news_podcast.api.lesson.schemas import CatalogTopic, LessonOptions
from news_podcast.api.lesson.state import (
    LessonContext,
    LessonProgress,
    SearchClient,
    Warn,
)
from tests.news_podcast.factories import build_lesson_options, catalog_topic
from tests.news_podcast.fake_models import FakeChatModel, fake_factory

# A Saturday.
TODAY = date(2026, 11, 14)
MODEL = "gemini-text"
# A workdir that doesn't exist, so a node that writes there fails the test.
NO_WORKDIR = Path("/nonexistent/news_podcast_lesson")

STORIES = [
    NewsStory("Câmara aprova a reforma", "Política", "o que muda para quem paga"),
    NewsStory("Congresso derruba veto do presidente", "Política", "como o veto caiu"),
]
POWERS = catalog_topic(
    id=11,
    area_id=1,
    area="Os Poderes e o Estado",
    area_position=1,
    title="Os três Poderes",
)
STJ = catalog_topic(
    id=21, area_id=2, area="Justiça", area_position=2, position=2, title="O STJ"
)
STF = catalog_topic(
    id=20,
    area_id=2,
    area="Justiça",
    area_position=2,
    title="O STF",
    taught_on=date(2026, 11, 1),
)
VETO = catalog_topic(
    id=31,
    area_id=3,
    area="O Congresso e as leis",
    area_position=3,
    title="Como o Congresso derruba um veto",
)
CATALOG = [POWERS, STF, STJ, VETO]
# Areas 1 and 3 were never taught; area 2 was.
FALLBACK = [POWERS, VETO, STJ]


def answer(candidate: int, story: int) -> str:
    return json.dumps({"candidate": candidate, "story": story})


class FakeSearch:
    """Answers the searches with `responses`, in order: an Exception raises. It
    records each query with its options, and whether the run closed it."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def search(self, query: str, **options: Any) -> dict[str, Any]:
        self.calls.append((query, options))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[no-any-return]

    async def __aenter__(self) -> "FakeSearch":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True


class RecordingTelegram(TelegramClient):
    """Records in `sent` each message as (chat, text) and each voice as (chat, path,
    caption, duration), and sends nothing."""

    def __init__(self, client: httpx.AsyncClient, sent: list[tuple[Any, ...]]) -> None:
        super().__init__(client, "TOKEN")
        self.sent = sent

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def send_voice(
        self, chat_id: str, voice_path: Path, caption: str, duration_seconds: int
    ) -> None:
        self.sent.append((chat_id, voice_path, caption, duration_seconds))


class FakeVoiceNote:
    """build_voice_note's stand-in, with no ffmpeg: it records each call's arguments
    and writes the file, which lasts `duration` seconds."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[Path], Path, float, Path]] = []
        self.duration = 545.6

    async def __call__(
        self, parts: list[Path], output: Path, speed: float, workdir: Path
    ) -> float:
        self.calls.append((parts, output, speed, workdir))
        output.write_bytes(b"ogg")
        return self.duration


def search_result(**changes: Any) -> dict[str, Any]:
    """A raw Tavily result that counts: a trusted host and enough text."""
    values = {
        "url": "https://www12.senado.leg.br/noticias/entenda-o-veto",
        "title": "Entenda o veto",
        "content": "O veto é a recusa do presidente a um projeto.",
        "raw_content": "Texto da página sobre o veto. " * 50,
        "published_date": "2025-03-10",
    }
    return {**values, **changes}


def found(*results: dict[str, Any]) -> dict[str, Any]:
    return {"query": "", "results": list(results)}


def script_answer(*words: int, changes: list[str] | None = None) -> str:
    """A script, or a reviewed one with `changes`: the hosts take turns, each turn
    with that many words. Ten turns of 100 words when no counts are given."""
    counts = words or (100,) * 10
    turns = [
        {"speaker": ("Bia", "Léo")[number % 2], "text": " ".join(["palavra"] * n)}
        for number, n in enumerate(counts)
    ]
    answer: dict[str, Any] = {"turns": turns}
    if changes is not None:
        answer["changes"] = changes
    return json.dumps(answer)


def words_in(total: int, turns: int = 10) -> tuple[int, ...]:
    """`total` words spread over `turns` turns."""
    base, extra = divmod(total, turns)
    return tuple(base + (1 if number < extra else 0) for number in range(turns))


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url}")


def offline_client() -> httpx.AsyncClient:
    """An httpx client that fails any request, so a test proves none is made."""
    return httpx.AsyncClient(transport=httpx.MockTransport(no_network))


def lesson_context(
    client: httpx.AsyncClient,
    catalog: Sequence[CatalogTopic],
    *,
    answers: list[Any] | None = None,
    asked: list[str] | None = None,
    built: dict[str, FakeChatModel] | None = None,
    options: LessonOptions | None = None,
    usages: Sequence[Usage] = (),
    search: SearchClient | None = None,
    warnings: list[str] | None = None,
    warn: Warn | None = None,
    telegram: TelegramClient | None = None,
    chat: str | None = None,
    admin_chat: str | None = None,
    workdir: Path = NO_WORKDIR,
    voice_waits: list[int] | None = None,
) -> LessonContext:
    """A lesson run's context over the fake chat model, which gives `answers` in
    order, and the fake search; the warnings land in `warnings`."""

    async def record(text: str) -> None:
        if warnings is not None:
            warnings.append(text)

    progress = LessonProgress(usages=list(usages))
    factory = fake_factory(
        {MODEL: answers if answers is not None else []},
        asked if asked is not None else [],
        built,
    )
    text = TextModels([MODEL], progress.usages, client, factory)
    return LessonContext(
        options=options or build_lesson_options(),
        catalog=tuple(catalog),
        today=TODAY,
        text=text,
        progress=progress,
        search=search,
        warn=warn or record,
        http_client=client,
        telegram=telegram,
        chat=chat,
        admin_chat=admin_chat,
        workdir=workdir,
        voice_waits=list(VOICE_RETRY_MINUTES) if voice_waits is None else voice_waits,
    )
