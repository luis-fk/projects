import asyncio
import logging
from argparse import ArgumentTypeError
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
        parser.add_argument(
            "--lesson-every-days",
            type=whole_days,
            help="Depois do episódio, uma aula a cada N dias (1 ou mais).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        configure_logging()
        send = not options["no_send"]
        try:
            if options["action"] == "chat-id":
                asyncio.run(report_chat_ids())
            elif options["action"] == "lesson":
                make_lesson(send=send)
            else:
                make_episode(
                    scheduled=options["scheduled"],
                    send=send,
                    every_days=options["lesson_every_days"],
                )
        except Exception as error:
            raise CommandError(str(error)) from error


def whole_days(value: str) -> int:
    """The lesson's cadence: a whole number of days, from 1."""
    try:
        days = int(value)
    except ValueError:
        raise ArgumentTypeError(f"{value} não é um número inteiro de dias") from None
    if days < 1:
        raise ArgumentTypeError("use 1 dia ou mais")
    return days


def make_episode(*, scheduled: bool, send: bool, every_days: int | None) -> None:
    """The daily episode, then, with every_days, the lesson when it is due. The
    news always goes first, each part reports its own failure, and the command
    fails naming each failed part."""
    news = run.make_episode(scheduled=scheduled, send=send)
    if news.paused:
        return
    failures = [f"episódio: {news.error}"] if news.error else []
    if every_days is not None:
        try:
            error = lesson_run.make_lesson(
                news.stories,
                send=send,
                news_sent=news.sent,
                scheduled=scheduled,
                every_days=every_days,
            )
        except Exception as raised:
            # Only the lesson's history write raises, and the news row is saved.
            logger.exception("The lesson's row couldn't be saved")
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
