import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from news_podcast.api.core.config import TEXT_RETRY_MINUTES, VOICE_RETRY_MINUTES
from news_podcast.api.core.llm import TextModels
from news_podcast.api.lesson import graph
from news_podcast.api.lesson.config import MAX_LESSON_MINUTES, MAX_LESSON_STEPS
from news_podcast.api.lesson.graph import LESSON_GRAPH, LessonOutcome, run_graph
from news_podcast.api.lesson.schemas import CatalogTopic, TopicToTry
from news_podcast.api.lesson.state import LessonError, LessonState
from tests.news_podcast.lesson.context import (
    CATALOG,
    STORIES,
    VETO,
    answer,
    lesson_context,
)

README = Path(graph.__file__).resolve().parents[2] / "README.md"
INPUTS: LessonState = {"stories": STORIES, "news_sent": True, "send": False}


# A broken time limit fails the test instead of hanging it.
TEST_SECONDS = 10


def run(catalog: list[CatalogTopic], answers: list[Any]) -> LessonOutcome:
    async def scenario() -> LessonOutcome:
        async with httpx.AsyncClient() as client:
            context = lesson_context(client, catalog, answers=answers)
            return await run_graph(INPUTS, context)

    return asyncio.run(asyncio.wait_for(scenario(), TEST_SECONDS))


def readme_diagram() -> str:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("## Lesson graph\n", 1)[1].split("\n## ", 1)[0]
    return section.split("```mermaid\n", 1)[1].split("```", 1)[0]


def test_the_readme_shows_the_graph_as_it_is() -> None:
    drawing = LESSON_GRAPH.get_graph()

    assert set(drawing.nodes) == {"__start__", "choose_topic", "__end__"}
    assert readme_diagram() == drawing.draw_mermaid()


def test_a_run_takes_the_topic_step_and_keeps_its_topics() -> None:
    outcome = run(CATALOG, [answer(3, 2)])

    assert outcome.error is None
    assert outcome.path == ["choose_topic"]
    assert outcome.state["topics"][0] == TopicToTry(VETO, STORIES[1])
    assert outcome.state["stories"] == STORIES


def test_a_choice_that_counts_as_none_still_counts_in_the_usages() -> None:
    outcome = run(CATALOG, [answer(0, 0)])

    assert [usage.step for usage in outcome.usages] == ["choice"]


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
