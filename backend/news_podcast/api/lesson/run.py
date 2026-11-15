"""Makes a lesson with the settings and the catalog from the admin and saves it in
the history, failed or not. The ORM runs only here, outside the event loop."""

import asyncio
import logging
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from django.conf import settings
from django.db import DatabaseError, IntegrityError, close_old_connections
from django.utils import timezone
from tavily import AsyncTavilyClient

from news_podcast.api.core.config import LOCAL_TZ, VOICE_RETRY_MINUTES
from news_podcast.api.core.costs import total_cost_usd
from news_podcast.api.core.llm import ModelFactory, TextModels, chat_model
from news_podcast.api.core.schemas import ModelPrice, NewsStory, Usage
from news_podcast.api.core.telegram import (
    admin_chat_id,
    chat_id,
    report_failure,
    telegram_client,
)
from news_podcast.api.lesson.graph import LessonOutcome, run_graph
from news_podcast.api.lesson.messages import (
    FAILURE_SUBJECT,
    admin_warner,
    log_warning,
    topic_gone_warning,
)
from news_podcast.api.lesson.schemas import CatalogTopic, LessonOptions
from news_podcast.api.lesson.state import LessonContext, LessonProgress, LessonState
from news_podcast.models import (
    Episode,
    LessonSettings,
    PodcastSettings,
    SettingsMissing,
    Topic,
)

logger = logging.getLogger(__name__)


def lesson_options(
    podcast_settings: PodcastSettings, lesson_settings: LessonSettings
) -> LessonOptions:
    """The lesson's own settings, with the speech speed from Settings."""
    return LessonOptions(
        min_minutes=lesson_settings.min_minutes,
        max_minutes=lesson_settings.max_minutes,
        speech_speed=podcast_settings.speech_speed,
        voice_model=lesson_settings.voice_model,
        voice_style=lesson_settings.voice_style,
        choice_prompt=lesson_settings.choice_prompt,
        pick_prompt=lesson_settings.pick_prompt,
        script_prompt=lesson_settings.script_prompt,
        review_prompt=lesson_settings.review_prompt,
    )


def make_lesson(
    stories: Sequence[NewsStory],
    *,
    send: bool,
    news_sent: bool,
    usages: Sequence[Usage] = (),
    model_factory: ModelFactory = chat_model,
) -> Exception | None:
    """Makes the lesson and saves it in the history, failed or not, and returns its
    error, which was already reported. `usages` are answers paid for before the
    lesson that belong to its row, such as the day's selection."""
    # A news episode may have run for up to 2 hours before this, and Django only
    # renews connections between requests.
    close_old_connections()
    try:
        podcast_settings = PodcastSettings.load()
        lesson_settings = LessonSettings.load()
        catalog = Topic.catalog()
    except (DatabaseError, SettingsMissing) as error:
        logger.error(f"The lesson couldn't read its settings: {error}")
        if send:
            asyncio.run(report_failure(FAILURE_SUBJECT, "configurações", error))
        return error

    started_at = timezone.now()
    inputs: LessonState = {
        "stories": list(stories),
        "news_sent": news_sent,
        "send": send,
    }
    progress = LessonProgress(usages=list(usages))
    try:
        outcome = asyncio.run(
            run_lesson(
                inputs,
                lesson_options(podcast_settings, lesson_settings),
                catalog,
                progress,
                text_models=podcast_settings.text_models(),
                model_factory=model_factory,
            )
        )
    except Exception as error:
        # The graph's own failures come back in its outcome: this is the run's
        # clients failing, which still gets its row and its notice.
        logger.exception(f"The lesson's run failed at {progress.step}")
        if send:
            asyncio.run(report_failure(FAILURE_SUBJECT, progress.step, error))
        outcome = LessonOutcome(
            inputs, error, progress.step, list(progress.path), progress.usages
        )
    # The lesson can last up to MAX_LESSON_MINUTES: the write opens a new connection
    # if the database closed the one that read the settings.
    close_old_connections()
    save_lesson(
        outcome,
        started_at=started_at,
        send=send,
        prices={**podcast_settings.prices(), **lesson_settings.prices()},
    )
    return outcome.error


async def run_lesson(
    inputs: LessonState,
    options: LessonOptions,
    catalog: Sequence[CatalogTopic],
    progress: LessonProgress,
    *,
    text_models: Sequence[str],
    model_factory: ModelFactory,
) -> LessonOutcome:
    """Runs the graph with clients built inside this run, so its waits, model marks
    and connections are its own, and a workdir that outlives the voice's parts."""
    send = bool(inputs.get("send"))
    async with httpx.AsyncClient() as http_client, search_client() as search:
        telegram = telegram_client(http_client)
        admin_chat = admin_chat_id()
        with tempfile.TemporaryDirectory(prefix="news_podcast_lesson_") as workdir:
            context = LessonContext(
                options=options,
                catalog=tuple(catalog),
                today=datetime.now(LOCAL_TZ).date(),
                text=TextModels(
                    text_models, progress.usages, http_client, model_factory
                ),
                progress=progress,
                search=search,
                warn=admin_warner(telegram, admin_chat) if send else log_warning,
                http_client=http_client,
                telegram=telegram,
                chat=chat_id(),
                admin_chat=admin_chat,
                workdir=Path(workdir),
                voice_waits=list(VOICE_RETRY_MINUTES),
            )
            return await run_graph(inputs, context)


@asynccontextmanager
async def search_client() -> AsyncIterator[AsyncTavilyClient | None]:
    """Tavily's client, with an HTTP client of its own, since it sets its key on
    the client's headers, and closed when the run ends. Without the key there is
    none, and the search fails the lesson."""
    api_key = getattr(settings, "TAVILY_API_KEY", None)
    if not api_key:
        yield None
        return
    async with AsyncTavilyClient(api_key=api_key) as client:
        yield client


def save_lesson(
    outcome: LessonOutcome,
    *,
    started_at: datetime,
    send: bool,
    prices: Mapping[str, ModelPrice],
) -> None:
    """The lesson's row. Its topic, story and pages come from the latest state once
    keep_pages ran, so a failed lesson keeps them too. A topic deleted in the admin
    during the run fails the write at its foreign key, and the row goes again with
    no topic and the topic's title in the error; any other failure reaches the
    caller."""
    state = outcome.state
    kept = state.get("topic")
    if outcome.error:
        status = Episode.Status.FAILED
    else:
        status = Episode.Status.SENT if send else Episode.Status.NOT_SENT
    error = f"{outcome.step}: {outcome.error}" if outcome.error else ""
    values = {
        "kind": Episode.Kind.LESSON,
        "started_at": started_at,
        "status": status,
        "duration_seconds": state.get("duration_seconds"),
        "script_words": state.get("words"),
        # The lesson's voice prices win for its own voice model.
        "cost_usd": total_cost_usd(outcome.usages, prices),
        "usage": [usage.model_dump() for usage in outcome.usages],
        "story_title": kept.hook.title if kept and kept.hook else "",
        "pages": [
            {"title": page.title, "url": page.url} for page in state.get("pages", [])
        ],
    }
    if kept is None:
        Episode.objects.create(**values, topic_id=None, error=error)
        return
    try:
        Episode.objects.create(**values, topic_id=kept.topic.id, error=error)
    except IntegrityError:
        if Topic.objects.filter(pk=kept.topic.id).exists():
            raise
        title = kept.topic.title
        logger.exception(f"Writing the lesson with its topic failed: {title}")
        Episode.objects.create(
            **values, topic_id=None, error=without_topic(error, title)
        )
        warn = admin_warning if send else log_warning
        asyncio.run(warn(topic_gone_warning(title)))


def without_topic(error: str, title: str) -> str:
    note = f"tema apagado: {title}"
    return f"{error} ({note})" if error else note


async def admin_warning(text: str) -> None:
    """A warning after the run, whose clients are closed by then: it goes over a
    client of its own, as report_failure does."""
    async with httpx.AsyncClient() as http_client:
        await admin_warner(telegram_client(http_client), admin_chat_id())(text)
