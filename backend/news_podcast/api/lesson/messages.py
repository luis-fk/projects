"""The lesson's caption and its warnings to the admin chat, which escape HTML and
never fail the lesson."""

import html
import logging
import math
from collections.abc import Sequence

from news_podcast.api.core.telegram import TelegramClient, TelegramError
from news_podcast.api.lesson.config import (
    LESSON_MINUTES_LIMIT,
    LESSON_WORDS_PER_MINUTE,
    MIN_LESSON_PAGES,
)
from news_podcast.api.lesson.schemas import CatalogTopic
from news_podcast.api.lesson.state import Warn

logger = logging.getLogger(__name__)

FAILURE_SUBJECT = "a aula de hoje"


def lesson_caption(topic: CatalogTopic) -> str:
    """One line, like the daily's: the topic, with no sources."""
    return f"🎓 <b>Aula · {html.escape(topic.title)}</b>"


def skipped_warning(titles: Sequence[str]) -> str:
    topics = ", ".join(f"<b>{html.escape(title)}</b>" for title in titles)
    return (
        f"⚠️ A aula de hoje pulou {topics}: a busca não achou pelo menos "
        f"{MIN_LESSON_PAGES} páginas boas. Se isso se repetir, desmarque o tema no "
        "admin."
    )


def long_lesson_warning(duration_seconds: float) -> str:
    # Rounded up, so a lesson just over the limit never reads as 10:00.
    minutes, seconds = divmod(math.ceil(duration_seconds), 60)
    return (
        f"⚠️ A aula de hoje saiu com {minutes}:{seconds:02d} min, acima de "
        f"{LESSON_MINUTES_LIMIT} minutos. Baixe os minutos máximos em Lesson "
        "settings, no admin, ou recalibre LESSON_WORDS_PER_MINUTE "
        f"({LESSON_WORDS_PER_MINUTE}) pelas palavras por minuto do histórico."
    )


def topic_gone_warning(title: str) -> str:
    return (
        f"⚠️ O tema <b>{html.escape(title)}</b> foi apagado no admin durante a aula de "
        "hoje, então o histórico guardou a aula sem o tema, com o título no erro."
    )


def admin_warner(telegram: TelegramClient | None, chat: str | None) -> Warn:
    """Sends each warning to the admin chat. A refused send goes to the log."""

    async def warn(text: str) -> None:
        if telegram is None or not chat:
            logger.warning(f"Lesson warning, with no admin chat: {text}")
            return
        try:
            await telegram.send_message(chat, text)
        except TelegramError as error:
            logger.error(f"Could not send the lesson's warning on Telegram: {error}")

    return warn


async def log_warning(text: str) -> None:
    """The warnings with --no-send."""
    logger.warning(f"Lesson warning: {text}")
