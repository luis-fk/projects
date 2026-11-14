import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import pytest
from news_podcast.api.lesson.script import review_script, word_range, write_script

from news_podcast.api.core.llm import TextError
from news_podcast.api.lesson.prompts import REVIEW_FIELDS, SCRIPT_FIELDS
from news_podcast.api.lesson.schemas import LessonOptions, LessonPage, TopicToTry
from news_podcast.api.lesson.state import LessonContext, LessonState, topic_start
from tests.news_podcast.factories import build_lesson_options, catalog_topic
from tests.news_podcast.fake_models import FakeChatModel
from tests.news_podcast.lesson.context import (
    MODEL,
    STORIES,
    lesson_context,
    offline_client,
    script_answer,
    words_in,
)

Node = Callable[[LessonState, LessonContext], Awaitable[LessonState]]
AREA: dict[str, Any] = {
    "area_id": 3,
    "area": "O Congresso e as leis",
    "area_position": 3,
}
VETO = catalog_topic(
    id=31, position=11, title="Como o Congresso derruba um veto", **AREA
)
CATALOG = [
    catalog_topic(
        id=30, position=1, title="Câmara e Senado", taught_on=date(2026, 11, 1), **AREA
    ),
    VETO,
    catalog_topic(
        id=32,
        position=2,
        title="Como um projeto vira lei",
        taught_on=date(2026, 11, 5),
        **AREA,
    ),
    catalog_topic(
        id=33, position=12, title="Lei ordinária, lei complementar e decreto", **AREA
    ),
    catalog_topic(id=34, position=13, title="Medida provisória", active=False, **AREA),
    catalog_topic(id=40, area_id=4, area="Eleições", area_position=4, title="O TSE"),
]
PAGES = [
    LessonPage(
        title="Entenda o veto",
        site="www12.senado.leg.br",
        url="https://www12.senado.leg.br/veto",
        published="2026-04-30",
        excerpt="O veto é a recusa do presidente.",
        text="Texto do Senado sobre o veto.",
    ),
    LessonPage(
        title="Vetos",
        site="www.congressonacional.leg.br",
        url="https://www.congressonacional.leg.br/vetos",
        published=None,
        excerpt="Lista de vetos.",
        text="Texto do Congresso.",
    ),
]
HOOKED = TopicToTry(VETO, STORIES[1])
STATE: LessonState = {
    "stories": STORIES,
    "news_sent": True,
    "topics": [HOOKED],
    **topic_start(0),
    "skipped": [],
    "topic": HOOKED,
    "pages": PAGES,
}
PAGES_TEXT = (
    "Páginas:\n"
    "1. Entenda o veto (www12.senado.leg.br, 2026-04-30)\n"
    "Trecho: O veto é a recusa do presidente.\n"
    "Texto:\n"
    "Texto do Senado sobre o veto.\n"
    "\n"
    "2. Vetos (www.congressonacional.leg.br, sem data)\n"
    "Trecho: Lista de vetos.\n"
    "Texto:\n"
    "Texto do Congresso."
)


def run_node(
    node: Node,
    state: LessonState,
    answers: list[Any],
    options: LessonOptions | None = None,
) -> tuple[LessonState, LessonContext, list[str], dict[str, FakeChatModel]]:
    async def scenario() -> tuple[
        LessonState, LessonContext, list[str], dict[str, FakeChatModel]
    ]:
        asked: list[str] = []
        built: dict[str, FakeChatModel] = {}
        async with offline_client() as client:
            context = lesson_context(
                client,
                CATALOG,
                answers=answers,
                asked=asked,
                built=built,
                options=options,
            )
            return await node(state, context), context, asked, built

    return asyncio.run(scenario())


def messages(built: dict[str, FakeChatModel], call: int = 0) -> tuple[str, str]:
    system, human = built[MODEL].received[call]
    return str(system.content), str(human.content)


@pytest.mark.parametrize(("speed", "words"), [(1.0, (900, 1350)), (1.5, (1350, 2025))])
def test_the_word_range_is_the_minutes_at_150_words_times_the_speed(
    speed: float, words: tuple[int, int]
) -> None:
    options = build_lesson_options(min_minutes=6, max_minutes=9, speech_speed=speed)

    assert word_range(options) == words


def test_the_script_gets_the_prompt_the_pages_the_story_and_the_area() -> None:
    state, context, asked, built = run_node(write_script, STATE, [script_answer()])

    system, human = messages(built)
    assert system == (
        "Bia explica e Léo pergunta. Hoje é sábado, 14 de novembro de 2026: Como o "
        "Congresso derruba um veto, de O Congresso e as leis, de 900 a 1350 palavras. "
        "Resumo enviado: sim."
    )
    assert human == (
        "Notícia de hoje que abre a aula: Congresso derruba veto do presidente "
        "(como o veto caiu)\n"
        "\n"
        "Aulas já dadas nesta área, na ordem: Câmara e Senado; Como um projeto vira "
        "lei\n"
        "Próximos temas da área: Lei ordinária, lei complementar e decreto\n"
        "\n" + PAGES_TEXT
    )
    assert (len(state["turns"]), state["words"]) == (10, 1000)
    assert [usage.step for usage in context.progress.usages] == ["script"]
    assert asked == [MODEL]


def test_a_topic_with_no_hook_has_no_story_part() -> None:
    plain: LessonState = {**STATE, "topic": TopicToTry(VETO, None)}

    _, _, _, built = run_node(write_script, plain, [script_answer()])

    assert "Notícia de hoje" not in messages(built)[1]


def test_news_sent_fills_the_prompt_with_no_when_the_summary_failed() -> None:
    failed_news: LessonState = {**STATE, "news_sent": False}

    _, _, _, built = run_node(write_script, failed_news, [script_answer()])

    assert messages(built)[0].endswith("Resumo enviado: não.")


def test_a_short_script_is_kept_with_no_second_request() -> None:
    state, _, asked, _ = run_node(write_script, STATE, [script_answer(*words_in(500))])

    assert state["words"] == 500
    assert asked == [MODEL]


def test_a_script_over_one_tenth_above_the_maximum_is_asked_again() -> None:
    answers = [script_answer(*words_in(1486)), script_answer(*words_in(1485))]

    state, _, asked, _ = run_node(write_script, STATE, answers)

    assert state["words"] == 1485
    assert asked == [MODEL, MODEL]


def test_a_script_with_too_few_turns_is_a_bad_answer() -> None:
    short = script_answer(*words_in(900, turns=9))

    with pytest.raises(TextError):
        run_node(write_script, STATE, [short, short])


def written(words: int = 1000) -> LessonState:
    state, _, _, _ = run_node(write_script, STATE, [script_answer(*words_in(words))])
    return {**STATE, **state}


def test_the_review_gets_the_pages_the_story_and_the_script(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewed = script_answer(*words_in(990), changes=["Tirei uma taxa sem data."])

    with caplog.at_level(logging.INFO):
        state, context, asked, built = run_node(review_script, written(), [reviewed])

    system, human = messages(built)
    assert system == (
        "Hoje é sábado, 14 de novembro de 2026. Revise a aula sobre Como o Congresso "
        "derruba um veto contra as páginas."
    )
    assert human.startswith(
        PAGES_TEXT
        + "\n\nNotícia que abriu a aula: Congresso derruba veto do presidente "
        "(como o veto caiu)\n\nRoteiro:\nBia: palavra"
    )
    assert human.count("\nBia: ") + human.count("\nLéo: ") == 10
    assert state["words"] == 990
    assert "Tirei uma taxa sem data." in caplog.text
    assert [usage.step for usage in context.progress.usages] == ["review"]
    assert asked == [MODEL]


def test_a_review_more_than_5_percent_longer_is_asked_again() -> None:
    answers = [
        script_answer(*words_in(1051), changes=[]),
        script_answer(*words_in(1050), changes=[]),
    ]

    state, _, asked, _ = run_node(review_script, written(1000), answers)

    assert state["words"] == 1050
    assert asked == [MODEL, MODEL]


def test_a_review_with_too_few_turns_is_a_bad_answer() -> None:
    short = script_answer(*words_in(900, turns=9), changes=[])

    with pytest.raises(TextError):
        run_node(review_script, written(), [short, short])


@pytest.mark.parametrize(
    ("node", "field", "fields", "answer"),
    [
        (write_script, "script_prompt", SCRIPT_FIELDS, script_answer()),
        (review_script, "review_prompt", REVIEW_FIELDS, script_answer(changes=[])),
    ],
)
def test_every_script_and_review_placeholder_is_filled(
    node: Node, field: str, fields: tuple[str, ...], answer: str
) -> None:
    prompt = " ".join(f"{{{name}}}" for name in fields)
    options = build_lesson_options(**{field: prompt})

    _, _, _, built = run_node(node, written(), [answer], options)

    assert "{" not in messages(built)[0]
