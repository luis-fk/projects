import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from tavily.errors import UsageLimitExceededError

from news_podcast.api.core.config import TEXT_RETRY_MINUTES, VOICE_RETRY_MINUTES
from news_podcast.api.core.llm import TextError, TextModels
from news_podcast.api.core.telegram import TelegramClient, TelegramError
from news_podcast.api.lesson import graph
from news_podcast.api.lesson.config import MAX_LESSON_MINUTES, MAX_LESSON_STEPS
from news_podcast.api.lesson.graph import LESSON_GRAPH, LessonOutcome, run_graph
from news_podcast.api.lesson.messages import admin_warner
from news_podcast.api.lesson.schemas import CatalogTopic, TopicToTry
from news_podcast.api.lesson.sources import SearchError
from news_podcast.api.lesson.state import LessonError, LessonState, Warn
from tests.news_podcast.fake_models import FakeChatModel
from tests.news_podcast.lesson.context import (
    CATALOG,
    MODEL,
    POWERS,
    STORIES,
    VETO,
    FakeSearch,
    answer,
    found,
    lesson_context,
    offline_client,
    script_answer,
    search_result,
    words_in,
)

README = Path(graph.__file__).resolve().parents[2] / "README.md"
INPUTS: LessonState = {"stories": STORIES, "news_sent": True, "send": False}


# A broken time limit fails the test instead of hanging it.
TEST_SECONDS = 10


PAGES = [
    search_result(),
    search_result(url="https://www.camara.leg.br/veto", title="Vetos"),
    search_result(url="https://portal.stf.jus.br/veto", title="O STF e o veto"),
]


def pick(pages: list[int], terms: str = "") -> str:
    return json.dumps({"pages": pages, "terms": terms})


# The script and its review, after the pages.
WRITTEN = [script_answer(), script_answer(*words_in(990), changes=["Cortei."])]
TAIL = ["keep_pages", "write_script", "review_script"]


def run(
    catalog: list[CatalogTopic],
    answers: list[Any],
    search: FakeSearch | None = None,
    warnings: list[str] | None = None,
    built: dict[str, FakeChatModel] | None = None,
    warn: Callable[[httpx.AsyncClient], Warn] | None = None,
) -> LessonOutcome:
    async def scenario() -> LessonOutcome:
        async with offline_client() as client:
            context = lesson_context(
                client,
                catalog,
                answers=answers,
                search=search,
                warnings=warnings,
                built=built,
                warn=warn(client) if warn else None,
            )
            return await run_graph(INPUTS, context)

    return asyncio.run(asyncio.wait_for(scenario(), TEST_SECONDS))


def readme_diagram() -> str:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("## Lesson graph\n", 1)[1].split("\n## ", 1)[0]
    return section.split("```mermaid\n", 1)[1].split("```", 1)[0]


def test_the_readme_shows_the_graph_as_it_is() -> None:
    drawing = LESSON_GRAPH.get_graph()

    assert set(drawing.nodes) == {
        "__start__",
        "choose_topic",
        "search",
        "pick_pages",
        "next_topic",
        "keep_pages",
        "write_script",
        "review_script",
        "__end__",
    }
    assert readme_diagram() == drawing.draw_mermaid()


def test_one_good_search_settles_the_topic_and_its_pages_then_the_script() -> None:
    answers = [answer(3, 2), pick([1, 2, 3]), *WRITTEN]

    outcome = run(CATALOG, answers, FakeSearch(found(*PAGES)))

    assert outcome.error is None
    assert outcome.path == ["choose_topic", "search", "pick_pages", *TAIL]
    # The review's words, not the script's.
    assert (len(outcome.state["turns"]), outcome.state["words"]) == (10, 990)
    assert outcome.state["topic"] == TopicToTry(VETO, STORIES[1])
    assert [page.title for page in outcome.state["pages"]] == [
        "Entenda o veto",
        "Vetos",
        "O STF e o veto",
    ]
    assert outcome.state["stories"] == STORIES


def test_other_terms_bring_one_more_search_whose_pick_sees_every_result() -> None:
    fake = FakeSearch(found(PAGES[0]), found(PAGES[1], PAGES[2], PAGES[0]))
    answers = [answer(3, 2), pick([1], "derrubada de veto"), pick([1, 2, 3]), *WRITTEN]
    built: dict[str, FakeChatModel] = {}

    outcome = run(CATALOG, answers, fake, built=built)

    assert outcome.path == [
        "choose_topic",
        "search",
        "pick_pages",
        "search",
        "pick_pages",
        *TAIL,
    ]
    assert fake.calls[1][0] == "derrubada de veto"
    assert [result.url for result in outcome.state["results"]] == [
        page["url"] for page in PAGES
    ]
    second_pick = str(built[MODEL].received[2][1].content)
    assert [line[:3] for line in second_pick.splitlines()[1:]] == ["1. ", "2. ", "3. "]
    assert "Entenda o veto" in second_pick


def test_terms_on_the_second_pick_start_no_third_search() -> None:
    fake = FakeSearch(found(PAGES[0]), found(*PAGES))
    answers = [answer(3, 2), pick([1], "uma"), pick([1, 2], "outra"), *WRITTEN]

    outcome = run(CATALOG, answers, fake)

    assert outcome.error is None
    assert len(fake.calls) == 2
    assert outcome.path[-3:] == TAIL


def test_a_topic_short_of_pages_gives_way_to_the_next_with_one_warning() -> None:
    warnings: list[str] = []
    fake = FakeSearch(found(PAGES[0]), found(*PAGES))
    answers = [answer(3, 2), pick([1]), pick([1, 2, 3]), *WRITTEN]

    outcome = run(CATALOG, answers, fake, warnings)

    assert outcome.path == [
        "choose_topic",
        "search",
        "pick_pages",
        "next_topic",
        "search",
        "pick_pages",
        *TAIL,
    ]
    assert outcome.state["topic"] == TopicToTry(POWERS, None)
    assert fake.calls[1][0] == POWERS.title
    assert len(warnings) == 1
    assert VETO.title in warnings[0]


class RefusingTelegram(TelegramClient):
    async def send_message(self, chat_id: str, text: str) -> None:
        raise TelegramError("bot bloqueado")


def test_a_refused_warning_never_stops_the_lesson(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeSearch(found(PAGES[0]), found(*PAGES))
    answers = [answer(3, 2), pick([1]), pick([1, 2, 3]), *WRITTEN]

    outcome = run(
        CATALOG,
        answers,
        fake,
        warn=lambda client: admin_warner(RefusingTelegram(client, "token"), "123"),
    )

    assert outcome.error is None
    assert outcome.path[-3:] == TAIL
    assert "bot bloqueado" in caplog.text


def test_two_topics_short_of_pages_fail_the_lesson_with_no_warning() -> None:
    warnings: list[str] = []
    fake = FakeSearch(*[found(PAGES[0])] * 4)
    answers = [answer(3, 2), pick([1], "a"), pick([1]), pick([1], "b"), pick([1])]

    outcome = run(CATALOG, answers, fake, warnings)

    assert isinstance(outcome.error, LessonError)
    assert str(outcome.error) == (
        "Nenhum tema teve pelo menos 2 páginas boas: "
        "Como o Congresso derruba um veto; Os três Poderes."
    )
    assert outcome.step == "busca das fontes"
    assert outcome.path == [
        "choose_topic",
        "search",
        "pick_pages",
        "search",
        "pick_pages",
        "next_topic",
        "search",
        "pick_pages",
        "search",
        "pick_pages",
        "next_topic",
    ]
    assert [usage.step for usage in outcome.usages] == ["choice"] + ["pick"] * 4
    assert len(fake.calls) == 4
    assert warnings == []


def test_a_search_error_stops_the_lesson_at_once() -> None:
    fake = FakeSearch(UsageLimitExceededError("créditos acabaram"))

    outcome = run(CATALOG, [answer(3, 2)], fake)

    assert isinstance(outcome.error, SearchError)
    assert outcome.step == "busca das fontes"
    assert outcome.path == ["choose_topic", "search"]
    assert len(fake.calls) == 1


def test_a_pick_no_model_answers_fails_the_lesson() -> None:
    fake = FakeSearch(found(*PAGES))

    outcome = run(CATALOG, [answer(3, 2), "não é json", "nem isto"], fake)

    assert isinstance(outcome.error, TextError)
    assert outcome.step == "busca das fontes"


def test_a_choice_that_counts_as_none_still_counts_in_the_usages() -> None:
    answers = [answer(0, 0), pick([1, 2]), *WRITTEN]

    outcome = run(CATALOG, answers, FakeSearch(found(*PAGES)))

    assert [usage.step for usage in outcome.usages] == [
        "choice",
        "pick",
        "script",
        "review",
    ]


@pytest.mark.parametrize(
    ("written", "step", "usages"),
    [
        (["não é json", "nem isto"], "roteiro", ["script", "script"]),
        (
            [script_answer(), "não é json", "nem isto"],
            "revisão",
            ["script", "review", "review"],
        ),
    ],
)
def test_a_failed_script_or_review_keeps_the_topic_pages_and_paid_usages(
    written: list[str], step: str, usages: list[str]
) -> None:
    answers = [answer(3, 2), pick([1, 2, 3]), *written]

    outcome = run(CATALOG, answers, FakeSearch(found(*PAGES)))

    assert isinstance(outcome.error, TextError)
    assert outcome.step == step
    assert outcome.state["topic"] == TopicToTry(VETO, STORIES[1])
    assert len(outcome.state["pages"]) == 3
    assert [usage.step for usage in outcome.usages] == ["choice", "pick", *usages]


def test_a_run_with_no_candidate_fails_at_the_topic_step_without_raising() -> None:
    outcome = run([], [])

    assert isinstance(outcome.error, LessonError)
    assert outcome.step == "escolha do tema"
    assert outcome.path == ["choose_topic"]
    assert outcome.state == INPUTS


def test_the_time_limit_cancels_the_running_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled: list[bool] = []

    async def never(*args: Any, **kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(TextModels, "ask", never)
    monkeypatch.setattr(graph, "MAX_LESSON_MINUTES", 0.001)

    async def scenario() -> tuple[LessonOutcome, list[bool], int]:
        async with httpx.AsyncClient() as client:
            outcome = await run_graph(INPUTS, lesson_context(client, CATALOG))
        await asyncio.sleep(0)
        # Read before asyncio.run ends, since it cancels whatever is left on its own.
        left = len(asyncio.all_tasks()) - 1
        return outcome, list(cancelled), left

    outcome, cancelled_by_then, left = asyncio.run(
        asyncio.wait_for(scenario(), TEST_SECONDS)
    )

    assert isinstance(outcome.error, LessonError)
    assert str(outcome.error) == "A aula passou de 0.001 minutos"
    assert outcome.step == "escolha do tema"
    assert cancelled_by_then == [True]
    assert left == 0


def test_a_timeout_inside_a_node_keeps_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = TimeoutError("o modelo não respondeu")

    async def times_out(*args: Any, **kwargs: Any) -> None:
        raise timeout

    monkeypatch.setattr(TextModels, "ask", times_out)

    outcome = run(CATALOG, [])

    assert outcome.error is timeout


def test_the_lesson_limit_outlasts_every_wait() -> None:
    waits = sum(TEXT_RETRY_MINUTES) + sum(VOICE_RETRY_MINUTES)

    assert MAX_LESSON_MINUTES > waits


def test_the_runner_caps_the_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Recorder:
        async def astream(
            self, inputs: LessonState, **options: Any
        ) -> AsyncIterator[Any]:
            calls.append(options)
            return
            yield

    monkeypatch.setattr(graph, "LESSON_GRAPH", Recorder())

    run(CATALOG, [])

    assert calls[0]["config"] == {"recursion_limit": MAX_LESSON_STEPS}
    assert calls[0]["stream_mode"] == "values"
