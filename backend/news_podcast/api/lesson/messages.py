"""The lesson's warnings to the admin chat, which escape HTML and never fail the
lesson."""

import html
import logging
from collections.abc import Sequence

from news_podcast.api.core.telegram import TelegramClient, TelegramError
from news_podcast.api.lesson.config import MIN_LESSON_PAGES
from news_podcast.api.lesson.state import Warn

logger = logging.getLogger(__name__)


def skipped_warning(titles: Sequence[str]) -> str:
    topics = ", ".join(f"<b>{html.escape(title)}</b>" for title in titles)
    return (
        f"⚠️ A aula de hoje pulou {topics}: a busca não achou pelo menos "
        f"{MIN_LESSON_PAGES} páginas boas. Se isso se repetir, desmarque o tema no "
        "admin."
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
