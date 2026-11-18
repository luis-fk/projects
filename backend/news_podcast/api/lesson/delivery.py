"""The lesson's last nodes: the voice, the voice note and the Telegram send."""

from typing import Literal

from news_podcast.api.core.audio import build_voice_note
from news_podcast.api.core.config import OUTPUT_DIR
from news_podcast.api.core.voice.narration import narrate
from news_podcast.api.core.voice.providers import voice_provider
from news_podcast.api.lesson.config import LESSON_MINUTES_LIMIT
from news_podcast.api.lesson.messages import lesson_caption, long_lesson_warning
from news_podcast.api.lesson.state import LessonContext, LessonError, LessonState


async def narrate_lesson(state: LessonState, context: LessonContext) -> LessonState:
    """The reviewed script read aloud, one WAV part per chunk. The voice is built
    here, so a voice model with no implementation or a missing key fails this step.
    The lesson has no headlines, so its style goes in both style arguments."""
    options = context.options
    parts = await narrate(
        voice_provider(options.voice_model, context.http_client),
        state["turns"],
        style=options.voice_style,
        headline_style=options.voice_style,
        workdir=context.workdir,
        usages=context.progress.usages,
        waits=context.voice_waits,
    )
    return {"parts": parts}


async def build_audio(state: LessonState, context: LessonContext) -> LessonState:
    """The voice note, named after today, at the speech speed from Settings. One
    over LESSON_MINUTES_LIMIT still goes out, with a warning."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    audio = OUTPUT_DIR / f"aula-{context.today:%Y-%m-%d}.ogg"
    duration = await build_voice_note(
        state["parts"], audio, context.options.speech_speed, context.workdir
    )
    if duration > LESSON_MINUTES_LIMIT * 60:
        await context.warn(long_lesson_warning(duration))
    return {"audio": audio, "duration_seconds": duration}


async def send_lesson(state: LessonState, context: LessonContext) -> LessonState:
    if context.telegram is None or not context.chat:
        raise LessonError(
            "TELEGRAM_BOT_TOKEN ou TELEGRAM_ADMIN_CHAT_ID não configurado"
        )
    await context.telegram.send_voice(
        context.chat,
        state["audio"],
        lesson_caption(state["topic"].topic),
        round(state["duration_seconds"]),
    )
    return {}


def after_audio(state: LessonState) -> Literal["sending", "not_sending"]:
    return "sending" if state.get("send") else "not_sending"
