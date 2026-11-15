"""What every node of the lesson graph shares: its state, its context, and the run's
progress, which stays out of the state because LangGraph drops the writes of a node
that raises."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol, TypedDict

import httpx

from news_podcast.api.core.llm import TextModels
from news_podcast.api.core.schemas import NewsStory, Turn, Usage
from news_podcast.api.core.telegram import TelegramClient
from news_podcast.api.lesson.schemas import (
    CatalogTopic,
    LessonOptions,
    LessonPage,
    SearchResult,
    TopicToTry,
)

# Sends a warning to the admin chat, or to the log with --no-send; it never fails.
Warn = Callable[[str], Awaitable[None]]


class LessonError(Exception):
    """A lesson that can't be made, with its reason in Portuguese."""


class LessonState(TypedDict, total=False):
    """What the lesson has found so far. A node returns only the keys it changes, as
    new values."""

    stories: list[NewsStory]
    news_sent: bool
    send: bool
    topics: list[TopicToTry]
    topic_index: int
    skipped: list[str]
    results: list[SearchResult]
    searches: int
    picked: list[SearchResult]
    terms: str
    topic: TopicToTry
    pages: list[LessonPage]
    turns: list[Turn]
    words: int
    parts: list[Path]
    audio: Path
    duration_seconds: float


class SearchClient(Protocol):
    """tavily-python's AsyncTavilyClient, or a fake with the same search."""

    async def search(self, query: str, **options: Any) -> dict[str, Any]: ...


def topic_start(index: int) -> LessonState:
    """The keys a topic starts with: its index, and no results, searches, picks or
    terms yet."""
    return {
        "topic_index": index,
        "results": [],
        "searches": 0,
        "picked": [],
        "terms": "",
    }


@dataclass
class LessonProgress:
    """The step running, the nodes run so far and every usage paid, which the
    history keeps even when a node fails."""

    step: str = "início"
    path: list[str] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)


@dataclass(frozen=True)
class LessonContext:
    """What the run gives every node: the settings and catalog snapshots, today's
    São Paulo date, the clients built inside the run, the chats, the progress, the
    folder for the voice's parts and the voice waits left."""

    options: LessonOptions
    catalog: tuple[CatalogTopic, ...]
    today: date
    text: TextModels
    progress: LessonProgress
    search: SearchClient | None
    warn: Warn
    http_client: httpx.AsyncClient
    telegram: TelegramClient | None
    chat: str | None
    admin_chat: str | None
    workdir: Path
    voice_waits: list[int]
