from news_podcast.api.core.messages import failure_message
from news_podcast.api.core.voice.base import VoiceError
from news_podcast.api.lesson.config import LESSON_WORDS_PER_MINUTE
from news_podcast.api.lesson.messages import (
    FAILURE_SUBJECT,
    lesson_caption,
    long_lesson_warning,
    topic_gone_warning,
)
from tests.news_podcast.factories import catalog_topic
from tests.news_podcast.lesson.context import VETO


def test_the_caption_is_one_line_with_the_topic_escaped() -> None:
    assert lesson_caption(VETO) == "🎓 <b>Aula · Como o Congresso derruba um veto</b>"
    assert lesson_caption(catalog_topic(title="Vetos & <emendas>")) == (
        "🎓 <b>Aula · Vetos &amp; &lt;emendas&gt;</b>"
    )


def test_a_long_lesson_warning_names_its_length_rounded_up_and_both_fixes() -> None:
    text = long_lesson_warning(630.0)

    assert "saiu com 10:30 min, acima de 10 minutos" in text
    assert "minutos máximos" in text
    assert f"LESSON_WORDS_PER_MINUTE ({LESSON_WORDS_PER_MINUTE})" in text
    assert "saiu com 11:00 min" in long_lesson_warning(659.2)


def test_the_deleted_topic_warning_escapes_its_title() -> None:
    text = topic_gone_warning("Vetos & <emendas>")

    assert "<b>Vetos &amp; &lt;emendas&gt;</b>" in text


def test_a_failed_lesson_is_reported_as_todays_lesson() -> None:
    text = failure_message(FAILURE_SUBJECT, "voz", VoiceError("a cota acabou"))

    assert text.startswith("⚠️ Não consegui gerar a aula de hoje.\n")
