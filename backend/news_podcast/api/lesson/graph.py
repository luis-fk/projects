"""The lesson as a LangGraph graph: its nodes, its edges and how a run goes. The only
module that imports langgraph; the nodes are plain async functions of the state and
the context."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from news_podcast.api.core.schemas import Usage
from news_podcast.api.core.telegram import notify_failure
from news_podcast.api.lesson.config import MAX_LESSON_MINUTES, MAX_LESSON_STEPS
from news_podcast.api.lesson.delivery import (
    after_audio,
    build_audio,
    narrate_lesson,
    send_lesson,
)
from news_podcast.api.lesson.messages import FAILURE_SUBJECT
from news_podcast.api.lesson.script import review_script, write_script
from news_podcast.api.lesson.sources import (
    after_pick,
    keep_pages,
    next_topic,
    pick_pages,
    search,
)
from news_podcast.api.lesson.state import LessonContext, LessonError, LessonState
from news_podcast.api.lesson.topics import choose_topic

logger = logging.getLogger(__name__)

Node = Callable[[LessonState, LessonContext], Awaitable[LessonState]]
Builder = StateGraph[LessonState, LessonContext, LessonState, LessonState]
LessonGraph = CompiledStateGraph[LessonState, LessonContext, LessonState, LessonState]

# Each node, the step the history names when it fails, and its function.
NODES: tuple[tuple[str, str, Node], ...] = (
    ("choose_topic", "escolha do tema", choose_topic),
    ("search", "busca das fontes", search),
    ("pick_pages", "busca das fontes", pick_pages),
    ("next_topic", "busca das fontes", next_topic),
    ("keep_pages", "busca das fontes", keep_pages),
    ("write_script", "roteiro", write_script),
    ("review_script", "revisão", review_script),
    ("narrate", "voz", narrate_lesson),
    ("build_audio", "montagem do áudio", build_audio),
    ("send", "envio", send_lesson),
)


def add_node(graph: Builder, name: str, step: str, node: Node) -> None:
    """Adds the node through a wrapper that records its step and its name. A function
    of its own, so each wrapper keeps its own node, and a plain closure, since
    LangGraph reads its signature to pass the runtime."""

    async def run(state: LessonState, runtime: Runtime[LessonContext]) -> LessonState:
        progress = runtime.context.progress
        progress.step = step
        progress.path.append(name)
        return await node(state, runtime.context)

    graph.add_node(name, run)


def build_graph() -> LessonGraph:
    graph: Builder = StateGraph(LessonState, context_schema=LessonContext)
    for name, step, node in NODES:
        add_node(graph, name, step, node)
    graph.add_edge(START, "choose_topic")
    graph.add_edge("choose_topic", "search")
    graph.add_edge("search", "pick_pages")
    # The path map's keys label the drawing's edges.
    graph.add_conditional_edges(
        "pick_pages",
        after_pick,
        {
            "other_terms": "search",
            "enough_pages": "keep_pages",
            "too_few_pages": "next_topic",
        },
    )
    graph.add_edge("next_topic", "search")
    graph.add_edge("keep_pages", "write_script")
    graph.add_edge("write_script", "review_script")
    graph.add_edge("review_script", "narrate")
    graph.add_edge("narrate", "build_audio")
    graph.add_conditional_edges(
        "build_audio", after_audio, {"sending": "send", "not_sending": END}
    )
    graph.add_edge("send", END)
    return graph.compile()


LESSON_GRAPH = build_graph()


@dataclass(frozen=True)
class LessonOutcome:
    """How a run ended: the latest state, the error (None when it went through), and
    the progress's step, path and usages."""

    state: LessonState
    error: Exception | None
    step: str
    path: list[str]
    usages: list[Usage]


async def run_graph(inputs: LessonState, context: LessonContext) -> LessonOutcome:
    """Runs the lesson within its time and step limits, keeping the latest state, so
    a failure still has what the earlier nodes found. A failure comes back in the
    outcome, and reaches the admin chat when the lesson was to be sent; only a
    cancellation from outside goes through."""
    state = inputs
    error: Exception | None = None
    limit = asyncio.timeout(MAX_LESSON_MINUTES * 60)
    progress = context.progress
    try:
        async with limit:
            async for part in LESSON_GRAPH.astream(
                inputs,
                context=context,
                stream_mode="values",
                version="v2",
                config={"recursion_limit": MAX_LESSON_STEPS},
            ):
                if part["type"] == "values":
                    state = part["data"]
    except Exception as caught:
        error = caught
        # A TimeoutError from inside a node keeps its own message.
        if limit.expired():
            error = LessonError(f"A aula passou de {MAX_LESSON_MINUTES} minutos")
        path = " → ".join(progress.path)
        logger.exception(f"The lesson failed at {progress.step} ({path})")
        if inputs.get("send"):
            await notify_failure(
                context.telegram,
                context.admin_chat,
                FAILURE_SUBJECT,
                progress.step,
                error,
            )
    return LessonOutcome(
        state, error, progress.step, list(progress.path), progress.usages
    )
