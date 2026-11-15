import html
import re
from typing import Any

from news_podcast.api.core.messages import failure_message
from news_podcast.api.core.telegram import CAPTION_LIMIT
from news_podcast.api.core.voice.base import VoiceError
from news_podcast.api.lesson.config import LESSON_WORDS_PER_MINUTE
from news_podcast.api.lesson.messages import (
    FAILURE_SUBJECT,
    lesson_caption,
    link_text,
    long_lesson_warning,
    topic_gone_warning,
)
from news_podcast.api.lesson.schemas import LessonPage
from tests.news_podcast.factories import catalog_topic
from tests.news_podcast.lesson.context import VETO


def lesson_page(**changes: Any) -> LessonPage:
    values = {
        "title": "Entenda o veto",
        "site": "www12.senado.leg.br",
        "url": "https://www12.senado.leg.br/noticias/entenda-o-veto",
        "published": "2025-03-10",
        "excerpt": "O veto é a recusa do presidente.",
        "text": "Texto da página.",
    }
    return LessonPage(**{**values, **changes})


PAGES = [
    lesson_page(),
    lesson_page(title="Vetos", site="www.camara.leg.br", url="https://camara.leg.br/v"),
    lesson_page(title="O STF e o veto", site="portal.stf.jus.br", url="https://stf/v"),
]


def visible(caption: str) -> str:
    """The caption as Telegram shows it: the tags, and so the hrefs, gone, and the
    entities read."""
    return html.unescape(re.sub(r"<[^>]*>", "", caption))


def units(text: str) -> int:
    """Telegram's count: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def links(caption: str) -> list[tuple[str, str]]:
    return re.findall(r'<a href="([^"]*)">([^<]*)</a>', caption)


def test_the_caption_names_the_lesson_and_its_area_and_links_each_page() -> None:
    caption = lesson_caption(VETO, PAGES)

    assert caption == (
        "🎓 <b>Aula · Como o Congresso derruba um veto</b>\n"
        "<i>O Congresso e as leis</i>\n"
        "\n"
        "Fontes:\n"
        '• <a href="https://www12.senado.leg.br/noticias/entenda-o-veto">'
        "Entenda o veto</a>\n"
        '• <a href="https://camara.leg.br/v">Vetos</a>\n'
        '• <a href="https://stf/v">O STF e o veto</a>'
    )


def test_a_long_caption_is_cut_before_escaping_and_fits_telegrams_limit() -> None:
    # The longest topic and area the admin allows, and titles and URLs far longer
    # than any page's.
    topic = catalog_topic(
        title=("Vetos & <emendas> " * 12)[:200], area=("Leis & <normas> " * 7)[:100]
    )
    url = ('https://www.camara.leg.br/busca?q="veto"&pagina=' + "9" * 400)[:400]
    pages = [
        lesson_page(title=('O "veto" & <a derrubada> ' * 30)[:500], url=url)
        for _ in range(3)
    ]

    caption = lesson_caption(topic, pages)

    shown = visible(caption)
    assert CAPTION_LIMIT - len(pages) < units(shown) <= CAPTION_LIMIT
    # An href with a raw quote would end early, and the regex would miss its link.
    assert len(links(caption)) == len(pages)
    assert re.findall(r"&(?!amp;|lt;|gt;|quot;|#x27;)", caption) == []
    assert "Aula · Vetos &amp; &lt;emendas&gt;" in caption
    assert "<i>Leis &amp; &lt;normas&gt;" in caption
    for href, text in links(caption):
        assert href == html.escape(url, quote=True)
        assert html.unescape(text).startswith('O "veto" & <a derrubada>')
        assert text.endswith("…")
        assert units(html.unescape(text)) >= units(pages[0].site)


def test_a_link_shows_its_title_cut_to_its_room_but_never_less_than_its_site() -> None:
    page = lesson_page(title="Como um veto é derrubado pelo Congresso")

    assert link_text(page, 100) == page.title
    assert link_text(page, 20) == "Como um veto é derr…"
    assert link_text(page, 19) == "Como um veto é der…"
    # Less room than the site's name: the site's name, so the link still says where
    # it goes.
    assert link_text(page, 18) == "www12.senado.leg.br"
    assert link_text(lesson_page(title="Vetos"), 3) == "Vetos"
    assert link_text(lesson_page(title="  "), 100) == "www12.senado.leg.br"


def test_a_cut_counts_what_telegram_counts_twice() -> None:
    page = lesson_page(title="🏛️ O Congresso 🏛️ derruba o veto do presidente")

    text = link_text(page, 20)

    # Each 🏛 is 2 units, so 19 characters would pass the room.
    assert text == "🏛️ O Congresso 🏛️…"
    assert units(text) == 20


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
