import asyncio
import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from news_podcast.api.core.telegram import report_chat_ids
from news_podcast.api.episode import run
from news_podcast.api.lesson import run as lesson_run

ACTIONS = ("episode", "lesson", "chat-id")
QUIET_LOGGERS = ("httpx", "httpcore", "trafilatura", "htmldate", "charset_normalizer")
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Gera o resumo diário de notícias em áudio e envia no Telegram."
    requires_system_checks: list[str] = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("action", nargs="?", default="episode", choices=ACTIONS)
        parser.add_argument("--scheduled", action="store_true")
        parser.add_argument("--no-send", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        configure_logging()
        send = not options["no_send"]
        try:
            if options["action"] == "chat-id":
                asyncio.run(report_chat_ids())
            elif options["action"] == "lesson":
                make_lesson(send=send)
            else:
                make_episode(scheduled=options["scheduled"], send=send)
        except Exception as error:
            raise CommandError(str(error)) from error


def make_episode(*, scheduled: bool, send: bool) -> None:
    """The daily episode, then, on the cron's run, the lesson, which Lesson settings'
    cadence lets through when one is due. The news always goes first, each part
    reports its own failure, and the command fails naming each failed part."""
    news = run.make_episode(scheduled=scheduled, send=send)
    if news.paused:
        return
    failures = [f"episódio: {news.error}"] if news.error else []
    if scheduled:
        try:
            error = lesson_run.make_lesson(
                news.stories, send=send, news_sent=news.sent, scheduled=True
            )
        except Exception as raised:
            # The lesson reports its own failures; this one escaped them, and the
            # news row is already saved.
            logger.exception("The lesson failed outside its own reporting")
            error = raised
        if error:
            failures.append(f"aula: {error}")
    if failures:
        raise CommandError("; ".join(failures))


def make_lesson(*, send: bool) -> None:
    """A lesson on its own, on the day's stories, which no episode sent. The
    selection never raises: without stories, the lesson follows the course's
    order."""
    selection = run.select_today()
    error = lesson_run.make_lesson(
        selection.stories, send=send, news_sent=False, usages=selection.usages
    )
    if error:
        raise error


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
