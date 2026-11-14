import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import httpx

from news_podcast.api.core.llm import TextModels
from news_podcast.api.core.schemas import NewsStory, Usage
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
    records each query with its options."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search(self, query: str, **options: Any) -> dict[str, Any]:
        self.calls.append((query, options))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[no-any-return]


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
    )
