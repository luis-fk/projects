import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from tavily.errors import TimeoutError as TavilyTimeoutError

from news_podcast.api.core.llm import TextError
from news_podcast.api.core.telegram import TelegramClient, TelegramError
from news_podcast.api.lesson.config import (
    LESSON_SOURCE_DOMAINS,
    MAX_PAGE_CHARS,
    MIN_PAGE_CHARS,
    SEARCH_RESULTS,
)
from news_podcast.api.lesson.messages import admin_warner, skipped_warning
from news_podcast.api.lesson.prompts import PICK_FIELDS
from news_podcast.api.lesson.schemas import LessonPage, TopicToTry
from news_podcast.api.lesson.sources import (
    SearchError,
    after_pick,
    counted_result,
    keep_pages,
    next_topic,
    pick_pages,
    search,
)
from news_podcast.api.lesson.state import (
    LessonContext,
    LessonError,
    LessonState,
    SearchClient,
    topic_start,
)
from tests.news_podcast.factories import build_lesson_options
from tests.news_podcast.fake_models import FakeChatModel
from tests.news_podcast.lesson.context import (
    CATALOG,
    MODEL,
    POWERS,
    STORIES,
    VETO,
    FakeSearch,
    found,
    lesson_context,
    offline_client,
    search_result,
)

Node = Callable[[LessonState, LessonContext], Awaitable[LessonState]]
TOPICS = [TopicToTry(VETO, STORIES[1]), TopicToTry(POWERS, None)]
START: LessonState = {
    "stories": STORIES,
    "topics": TOPICS,
    "skipped": [],
    **topic_start(0),
}
SENADO = search_result()
CAMARA = search_result(
    url="https://www.camara.leg.br/veto", title="Vetos", published_date=None
)
STF = search_result(url="https://portal.stf.jus.br/veto", title="O STF e o veto")


def pick(pages: list[int], terms: str = "") -> str:
    return json.dumps({"pages": pages, "terms": terms})


def run_node(
    node: Node,
    state: LessonState,
    *,
    search_client: SearchClient | None = None,
    answers: list[Any] | None = None,
    warnings: list[str] | None = None,
    options: Any = None,
) -> tuple[LessonState, LessonContext, dict[str, FakeChatModel]]:
    async def scenario() -> tuple[LessonState, LessonContext, dict[str, FakeChatModel]]:
        built: dict[str, FakeChatModel] = {}
        async with offline_client() as client:
            context = lesson_context(
                client,
                CATALOG,
                answers=answers,
                built=built,
                search=search_client,
                warnings=warnings,
                options=options,
            )
            return await node(state, context), context, built

    return asyncio.run(scenario())


def test_the_first_search_asks_for_the_topic_on_the_trusted_sites() -> None:
    fake = FakeSearch(found(SENADO))

    state, _, _ = run_node(search, START, search_client=fake)

    assert fake.calls == [
        (
            "Como o Congresso derruba um veto",
            {
                "search_depth": "advanced",
                "max_results": SEARCH_RESULTS,
                "include_domains": list(LESSON_SOURCE_DOMAINS),
                "include_raw_content": True,
                "include_published_date": True,
            },
        )
    ]
    assert (state["searches"], state["terms"]) == (1, "")


def test_only_trusted_hosts_with_enough_text_count() -> None:
    urls = [
        "https://www12.senado.leg.br/a",
        "https://www.politize.com.br/b",
        "https://agenciabrasil.ebc.com.br/c",
        "https://ebc.com.br/d",
        "https://example.com/e",
        "https://notleg.br/f",
        "https://leg.br.example.com/g",
    ]
    results = [search_result(url=url) for url in urls]
    results.append(search_result(url="https://www.gov.br/curta", raw_content="Pouco."))

    state, _, _ = run_node(search, START, search_client=FakeSearch(found(*results)))

    assert [result.site for result in state["results"]] == [
        "www12.senado.leg.br",
        "www.politize.com.br",
        "agenciabrasil.ebc.com.br",
    ]


def test_a_result_without_text_is_dropped_without_an_error() -> None:
    no_text = search_result(raw_content=None)
    missing = search_result(url="https://www.gov.br/x")
    del missing["raw_content"]

    assert counted_result(no_text) is None
    assert counted_result(missing) is None


@pytest.mark.parametrize(
    ("published", "day"),
    [
        ("Thu, 30 Apr 2026 18:53:56 GMT", "2026-04-30"),
        ("2025-03-10T08:00:00Z", "2025-03-10"),
        ("", None),
        ("ontem", "ontem"),
        (
            "Mon, 1 Jan 99999999999999999999 00:00:00 GMT",
            "Mon, 1 Jan 99999999999999999999 00:00:00 GMT",
        ),
    ],
)
def test_a_published_date_reads_as_a_day(published: str, day: str | None) -> None:
    result = counted_result(search_result(published_date=published))

    assert result is not None
    assert result.published == day


def test_a_real_search_replayed_keeps_subdomains_text_and_dates() -> None:
    """One real search with the production call, saved on 2026-11-14 with each text
    cut above MIN_PAGE_CHARS."""
    saved = json.loads((Path(__file__).parent / "real_search.json").read_text("utf-8"))

    state, _, _ = run_node(search, START, search_client=FakeSearch(saved))

    results = state["results"]
    assert len(results) == 8
    assert all(len(result.text) >= MIN_PAGE_CHARS for result in results)
    assert {result.site for result in results} >= {
        "www12.senado.leg.br",
        "www.camara.leg.br",
        "www.congressonacional.leg.br",
    }
    dated = [result for result in results if result.published]
    assert dated and len(dated) < len(results)
    assert all(len(str(result.published)) == 10 for result in dated)


def test_the_same_page_under_another_url_counts_once() -> None:
    first, _, _ = run_node(search, START, search_client=FakeSearch(found(SENADO)))
    again: LessonState = {**START, **first, "terms": "veto"}
    variants = [
        search_result(url=SENADO["url"] + "/"),
        search_result(url=SENADO["url"] + "#topo"),
        search_result(url=SENADO["url"].replace("www12.senado", "WWW12.Senado")),
    ]

    state, _, _ = run_node(search, again, search_client=FakeSearch(found(*variants)))

    assert [result.url for result in state["results"]] == [SENADO["url"]]


@pytest.mark.parametrize(
    "url",
    [
        "https://[gov.br/x",
        "javascript://www.gov.br/x",
        "https://evil.example\\@www.gov.br/x",
        None,
    ],
)
def test_an_odd_url_never_counts_nor_fails_the_search(url: str | None) -> None:
    assert counted_result(search_result(url=url)) is None


def test_a_title_that_isn_t_text_goes_by_the_url() -> None:
    result = counted_result(search_result(title=7))

    assert result is not None
    assert result.title == SENADO["url"]


def test_a_response_that_isn_t_an_object_is_a_search_error() -> None:
    with pytest.raises(SearchError):
        run_node(search, START, search_client=FakeSearch(["não é um objeto"]))


def test_a_body_that_isn_t_json_is_a_search_error() -> None:
    broken = json.JSONDecodeError("Expecting value", "<html>", 0)

    with pytest.raises(SearchError):
        run_node(search, START, search_client=FakeSearch(broken))


def test_a_result_that_isn_t_an_object_is_skipped() -> None:
    state, _, _ = run_node(
        search,
        START,
        search_client=FakeSearch(found("lixo", SENADO)),  # type: ignore[arg-type]
    )

    assert [result.url for result in state["results"]] == [SENADO["url"]]


@pytest.mark.parametrize(
    "error", [TavilyTimeoutError(60), httpx.ConnectError("sem rede")]
)
def test_a_search_error_carries_its_cause(error: Exception) -> None:
    with pytest.raises(SearchError) as caught:
        run_node(search, START, search_client=FakeSearch(error))

    assert type(error).__name__ in str(caught.value)
    assert isinstance(caught.value, LessonError)


def test_a_search_without_a_key_says_so() -> None:
    with pytest.raises(SearchError, match="TAVILY_API_KEY não configurada"):
        run_node(search, START, search_client=None)


def searched(*results: dict[str, Any]) -> LessonState:
    """The state after a first search, which a node returns only in part."""
    update, _, _ = run_node(search, START, search_client=FakeSearch(found(*results)))
    return {**START, **update}


def test_the_pick_gets_the_prompt_and_the_results_but_never_their_text() -> None:
    state = searched(SENADO, CAMARA)

    _, context, built = run_node(pick_pages, state, answers=[pick([1, 2])])

    system, human = built[MODEL].received[0]
    assert system.content == (
        "Hoje é sábado, 14 de novembro de 2026. Até 3 páginas sobre Como o "
        "Congresso derruba um veto, de O Congresso e as leis."
    )
    assert human.content == (
        "Resultados da busca:\n"
        "1. Entenda o veto (www12.senado.leg.br, 2025-03-10): O veto é a recusa do "
        "presidente a um projeto.\n"
        "2. Vetos (www.camara.leg.br, sem data): O veto é a recusa do presidente a "
        "um projeto."
    )
    assert "Texto da página" not in str(human.content)
    assert [usage.step for usage in context.progress.usages] == ["pick"]


def test_each_result_takes_one_line_in_the_pick_and_an_untitled_one_its_url() -> None:
    messy = search_result(
        title="Entenda\no veto",
        content="### Passo a passo\n\n1. O presidente veta\n2. O Congresso vota [...]",
    )
    untitled = search_result(url="https://www.camara.leg.br/sem-titulo", title=None)
    state = searched(messy, untitled)

    _, _, built = run_node(pick_pages, state, answers=[pick([1, 2])])

    human = str(built[MODEL].received[0][1].content)
    assert human.splitlines() == [
        "Resultados da busca:",
        "1. Entenda o veto (www12.senado.leg.br, 2025-03-10): ### Passo a passo 1. O "
        "presidente veta 2. O Congresso vota [...]",
        "2. https://www.camara.leg.br/sem-titulo (www.camara.leg.br, 2025-03-10): O "
        "veto é a recusa do presidente a um projeto.",
    ]


def test_the_pick_is_asked_even_with_no_result() -> None:
    state = searched()

    picked, _, built = run_node(
        pick_pages, state, answers=[pick([], "veto presidencial")]
    )

    assert built[MODEL].received[0][1].content == (
        "Resultados da busca:\nNenhum resultado dos sites confiáveis."
    )
    assert (picked["picked"], picked["terms"]) == ([], "veto presidencial")


def test_every_pick_placeholder_is_filled() -> None:
    prompt = " ".join(f"{{{name}}}" for name in PICK_FIELDS)
    options = build_lesson_options(pick_prompt=prompt)

    _, _, built = run_node(
        pick_pages, searched(SENADO), answers=[pick([1])], options=options
    )

    system = str(built[MODEL].received[0][0].content)
    assert "{" not in system
    assert system == (
        "sábado, 14 de novembro de 2026 Como o Congresso derruba um veto "
        "O Congresso e as leis 3"
    )


def test_the_pick_keeps_the_first_three_known_pages_best_first() -> None:
    fourth = search_result(url="https://www.gov.br/quarta", title="Quarta")
    state = searched(SENADO, CAMARA, STF, fourth)

    picked, _, _ = run_node(pick_pages, state, answers=[pick([4, 9, 1, 2, 1, 3])])

    assert [result.title for result in picked["picked"]] == [
        "Quarta",
        "Entenda o veto",
        "Vetos",
    ]


def test_a_blank_terms_answer_means_no_terms() -> None:
    picked, _, _ = run_node(pick_pages, searched(SENADO), answers=[pick([1], "   ")])

    assert picked["terms"] == ""


def test_terms_over_300_characters_are_a_bad_answer() -> None:
    long = pick([1], "x" * 301)

    with pytest.raises(TextError):
        run_node(pick_pages, searched(SENADO), answers=[long, long])


def test_keep_pages_cuts_each_text_and_settles_the_topic() -> None:
    long = search_result(raw_content="a" * (MAX_PAGE_CHARS + 500))
    found_state = searched(long, CAMARA)
    state: LessonState = {**found_state, "picked": list(found_state["results"])}

    kept, _, _ = run_node(keep_pages, state)

    assert kept["topic"] == TOPICS[0]
    assert kept["pages"] == [
        LessonPage(
            title="Entenda o veto",
            site="www12.senado.leg.br",
            url=SENADO["url"],
            published="2025-03-10",
            excerpt=SENADO["content"],
            text="a" * MAX_PAGE_CHARS,
        ),
        LessonPage(
            title="Vetos",
            site="www.camara.leg.br",
            url=CAMARA["url"],
            published=None,
            excerpt=CAMARA["content"],
            text=CAMARA["raw_content"],
        ),
    ]


def test_keep_pages_warns_once_only_when_a_topic_was_skipped() -> None:
    warnings: list[str] = []
    skipped = ["Os <três> Poderes & cia"]

    run_node(keep_pages, {**START, "picked": [], "skipped": []}, warnings=warnings)
    run_node(keep_pages, {**START, "picked": [], "skipped": skipped}, warnings=warnings)

    assert warnings == [skipped_warning(skipped)]
    assert "<b>Os &lt;três&gt; Poderes &amp; cia</b>" in warnings[0]


class RefusingTelegram(TelegramClient):
    async def send_message(self, chat_id: str, text: str) -> None:
        raise TelegramError("bot bloqueado")


def test_a_warning_that_telegram_refuses_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            warn = admin_warner(RefusingTelegram(client, "token"), "123")
            await warn("aviso")

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())

    assert "bot bloqueado" in caplog.text


def test_next_topic_starts_the_next_topic_and_remembers_the_skipped_one() -> None:
    state: LessonState = {**START, "results": [], "searches": 2, "terms": "x"}

    moved, _, _ = run_node(next_topic, state)

    assert moved == {**topic_start(1), "skipped": [VETO.title]}


@pytest.mark.parametrize(
    ("terms", "searches", "picked", "route"),
    [
        ("veto", 1, 3, "other_terms"),
        ("veto", 2, 3, "enough_pages"),
        ("", 1, 2, "enough_pages"),
        ("", 1, 1, "too_few_pages"),
    ],
)
def test_after_pick_tries_the_terms_then_counts_the_pages(
    terms: str, searches: int, picked: int, route: str
) -> None:
    result = counted_result(SENADO)
    assert result is not None
    state: LessonState = {
        "terms": terms,
        "searches": searches,
        "picked": [result] * picked,
    }

    assert after_pick(state) == route
