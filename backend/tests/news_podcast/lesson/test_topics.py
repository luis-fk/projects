import asyncio
from datetime import date
from typing import Any

import httpx
import pytest

from news_podcast.api.core.config import TEXT_RETRY_MINUTES
from news_podcast.api.core.schemas import NewsStory
from news_podcast.api.lesson.prompts import CHOICE_FIELDS
from news_podcast.api.lesson.schemas import CatalogTopic, TopicToTry
from news_podcast.api.lesson.state import LessonContext, LessonError, LessonState
from news_podcast.api.lesson.topics import candidates, choose_topic, fallback_order
from tests.news_podcast.factories import build_lesson_options, catalog_topic
from tests.news_podcast.fake_models import FakeChatModel
from tests.news_podcast.lesson.context import (
    CATALOG,
    FALLBACK,
    MODEL,
    POWERS,
    STF,
    STJ,
    STORIES,
    VETO,
    answer,
    lesson_context,
)


def choose(
    catalog: list[CatalogTopic], stories: list[NewsStory], answers: list[Any]
) -> tuple[LessonState, LessonContext, list[str], dict[str, FakeChatModel]]:
    async def scenario() -> tuple[
        LessonState, LessonContext, list[str], dict[str, FakeChatModel]
    ]:
        asked: list[str] = []
        built: dict[str, FakeChatModel] = {}
        async with httpx.AsyncClient() as client:
            context = lesson_context(
                client, catalog, answers=answers, asked=asked, built=built
            )
            state = await choose_topic(
                {"stories": stories, "news_sent": True, "send": False}, context
            )
        return state, context, asked, built

    return asyncio.run(scenario())


def tried(state: LessonState) -> list[TopicToTry]:
    return state["topics"]


def test_an_area_offers_its_first_active_untaught_topic() -> None:
    catalog = [
        catalog_topic(id=1, position=1, title="desativado", active=False),
        catalog_topic(id=2, position=2, title="dado", taught_on=date(2026, 11, 12)),
        catalog_topic(id=3, position=3, title="o próximo"),
        catalog_topic(id=4, position=4, title="depois"),
    ]

    assert [topic.title for topic in candidates(catalog)] == ["o próximo"]


def test_a_tie_in_position_goes_to_the_lower_id_every_time() -> None:
    catalog = [catalog_topic(id=7, title="sete"), catalog_topic(id=3, title="três")]

    for _ in range(2):
        assert [topic.title for topic in candidates(catalog)] == ["três"]


def test_a_used_up_area_offers_nothing_and_the_others_still_offer() -> None:
    catalog = [
        catalog_topic(id=1, area_id=1, area_position=1, active=False),
        catalog_topic(
            id=2, area_id=1, area_position=1, position=2, taught_on=date(2026, 11, 1)
        ),
        catalog_topic(id=3, area_id=2, area="Eleições", area_position=2, title="TSE"),
        catalog_topic(id=4, area_id=3, area="Economia", area_position=3, title="PIB"),
    ]

    assert [topic.title for topic in candidates(catalog)] == ["TSE", "PIB"]


def test_candidates_come_in_area_order_whatever_the_catalog_s_order() -> None:
    catalog = [
        catalog_topic(id=1, area_id=9, area_position=2, title="segunda área"),
        catalog_topic(id=2, area_id=5, area_position=1, title="primeira área"),
        catalog_topic(id=3, area_id=8, area_position=1, title="empate, id maior"),
    ]

    assert [topic.title for topic in candidates(catalog)] == [
        "primeira área",
        "empate, id maior",
        "segunda área",
    ]


def test_the_fallback_starts_with_untaught_areas_then_the_least_recent() -> None:
    catalog = [
        # Area 1, taught most recently.
        catalog_topic(id=1, area_id=1, area_position=1, taught_on=date(2026, 11, 13)),
        catalog_topic(id=2, area_id=1, area_position=1, position=2, title="área 1"),
        # Area 2, never taught.
        catalog_topic(id=3, area_id=2, area_position=2, title="área 2"),
        # Areas 3 and 4, taught on the same oldest day: area order breaks the tie.
        catalog_topic(id=4, area_id=4, area_position=4, taught_on=date(2026, 11, 2)),
        catalog_topic(id=5, area_id=4, area_position=4, position=2, title="área 4"),
        catalog_topic(id=6, area_id=3, area_position=3, taught_on=date(2026, 11, 2)),
        catalog_topic(id=7, area_id=3, area_position=3, position=2, title="área 3"),
        # Area 5, never taught, after area 2 in area order.
        catalog_topic(id=8, area_id=5, area_position=5, title="área 5"),
    ]

    ordered = fallback_order(candidates(catalog), catalog)

    assert [topic.title for topic in ordered] == [
        "área 2",
        "área 5",
        "área 3",
        "área 4",
        "área 1",
    ]


def test_a_clear_link_puts_the_candidate_first_with_its_story_as_the_hook() -> None:
    state, _, asked, _ = choose(CATALOG, STORIES, [answer(3, 2)])

    assert tried(state) == [
        TopicToTry(VETO, STORIES[1]),
        TopicToTry(POWERS, None),
        TopicToTry(STJ, None),
    ]
    assert (state["topic_index"], state["skipped"]) == (0, [])
    assert asked == [MODEL]


@pytest.mark.parametrize(
    "reply",
    [answer(9, 1), answer(3, 0), answer(3, 7), answer(0, 2)],
)
def test_no_clear_link_keeps_the_fallback_order_and_asks_once(reply: str) -> None:
    state, _, asked, _ = choose(CATALOG, STORIES, [reply])

    assert tried(state) == [TopicToTry(topic, None) for topic in FALLBACK]
    assert asked == [MODEL]


def test_a_choice_that_fails_falls_back_at_once_and_keeps_the_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)

    state, context, asked, _ = choose(CATALOG, STORIES, [TimeoutError()])

    assert tried(state) == [TopicToTry(topic, None) for topic in FALLBACK]
    assert asked == [MODEL]
    assert sleeps == []
    assert context.text.retry_minutes == list(TEXT_RETRY_MINUTES)


def test_the_choice_gets_the_prompt_the_candidates_and_the_numbered_stories() -> None:
    _, context, _, built = choose(CATALOG, STORIES, [answer(0, 0)])

    system, human = built[MODEL].received[0]
    assert system.content == (
        "Hoje é sábado, 14 de novembro de 2026. "
        "Escolha um tema só se ele se ligar à notícia."
    )
    assert human.content == (
        "Temas candidatos:\n"
        "1. Os três Poderes (Os Poderes e o Estado)\n"
        "2. O STJ (Justiça)\n"
        "3. Como o Congresso derruba um veto (O Congresso e as leis)\n"
        "\n"
        "Notícias de hoje, da mais importante para a menos importante:\n"
        "1. Câmara aprova a reforma (Política): o que muda para quem paga\n"
        "2. Congresso derruba veto do presidente (Política): como o veto caiu"
    )
    assert [usage.step for usage in context.progress.usages] == ["choice"]


def test_every_choice_placeholder_is_filled() -> None:
    prompt = " ".join(f"{{{name}}}" for name in CHOICE_FIELDS)
    options = build_lesson_options(choice_prompt=prompt)

    async def scenario() -> str:
        built: dict[str, FakeChatModel] = {}
        async with httpx.AsyncClient() as client:
            context = lesson_context(
                client, CATALOG, answers=[answer(0, 0)], built=built, options=options
            )
            await choose_topic({"stories": STORIES}, context)
        return str(built[MODEL].received[0][0].content)

    assert asyncio.run(scenario()) == "sábado, 14 de novembro de 2026"


@pytest.mark.parametrize(
    ("catalog", "message"),
    [
        ([], "O catálogo de temas está vazio: rode seed_news_podcast."),
        (
            [STF, catalog_topic(id=40, active=False)],
            "Nenhum tema disponível: todos foram dados ou estão desativados; "
            "cadastre novos temas no admin.",
        ),
    ],
)
def test_with_no_candidate_the_choice_fails_without_a_request(
    catalog: list[CatalogTopic], message: str
) -> None:
    asked: list[str] = []

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            context = lesson_context(client, catalog, asked=asked)
            await choose_topic({"stories": STORIES}, context)

    with pytest.raises(LessonError) as error:
        asyncio.run(scenario())

    assert str(error.value) == message
    assert asked == []


def test_a_choice_that_is_not_a_number_is_a_bad_answer() -> None:
    reply = '{"candidate": true, "story": true}'

    state, _, asked, _ = choose(CATALOG, STORIES, [reply, reply])

    assert tried(state) == [TopicToTry(topic, None) for topic in FALLBACK]
    assert asked == [MODEL, MODEL]
