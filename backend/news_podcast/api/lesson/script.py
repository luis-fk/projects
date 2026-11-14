"""The lesson's script, written from its pages only, then reviewed against them."""

import logging
from collections.abc import Callable, Sequence

from news_podcast.api.core.config import HOSTS
from news_podcast.api.core.llm import BadAnswerError
from news_podcast.api.core.schemas import NewsStory, Turn
from news_podcast.api.core.turns import checked_turns, count_words
from news_podcast.api.lesson.config import (
    LESSON_WORDS_PER_MINUTE,
    LONG_SCRIPT_RATIO,
    MAX_REVIEW_GROWTH,
)
from news_podcast.api.lesson.prompts import lesson_date, material_template, one_line
from news_podcast.api.lesson.schemas import (
    CatalogTopic,
    LessonOptions,
    LessonPage,
    LessonScript,
    LessonTurn,
    ReviewedScript,
    TopicToTry,
)
from news_podcast.api.lesson.state import LessonContext, LessonState
from news_podcast.api.lesson.topics import teaching_order

logger = logging.getLogger(__name__)


def word_range(options: LessonOptions) -> tuple[int, int]:
    """The minutes at the lesson's pace times the speech speed, as the daily's."""
    pace = round(LESSON_WORDS_PER_MINUTE * options.speech_speed)
    return options.min_minutes * pace, options.max_minutes * pace


async def write_script(state: LessonState, context: LessonContext) -> LessonState:
    topic = state["topic"]
    min_words, max_words = word_range(context.options)
    turns = await context.text.ask(
        material_template(context.options.script_prompt),
        {
            "material": script_material(topic, state["pages"], context.catalog),
            "host_a": HOSTS[0].name,
            "host_b": HOSTS[1].name,
            "today": lesson_date(context.today),
            "topic": topic.topic.title,
            "area": topic.topic.area,
            "min_words": min_words,
            "max_words": max_words,
            "news_sent": "sim" if state.get("news_sent") else "não",
        },
        step="script",
        schema=LessonScript,
        check=checked_script(max_words),
    )
    words = count_words(turns)
    logger.info(
        f"Lesson script: {len(turns)} turns, {words} words "
        f"(range {min_words} to {max_words})"
    )
    return {"turns": turns, "words": words}


async def review_script(state: LessonState, context: LessonContext) -> LessonState:
    """No review, no lesson: a failure here fails the lesson."""
    topic = state["topic"]
    script = state["turns"]
    _, max_words = word_range(context.options)
    turns, changes = await context.text.ask(
        material_template(context.options.review_prompt),
        {
            "material": review_material(topic, state["pages"], script, context.catalog),
            "today": lesson_date(context.today),
            "topic": topic.topic.title,
        },
        step="review",
        schema=ReviewedScript,
        check=checked_review(state["words"], len(script), max_words),
    )
    for change in changes:
        logger.info(f"Lesson review: {change}")
    if not changes and turns != script:
        logger.warning("The lesson review changed the script without listing a change")
    return {"turns": turns, "words": count_words(turns)}


def checked_script(max_words: int) -> Callable[[LessonScript], list[Turn]]:
    """The cleaned turns. A script over LONG_SCRIPT_RATIO times the maximum is asked
    again; a short one is fine, since there is no lengthening."""
    ceiling = round(LONG_SCRIPT_RATIO * max_words)

    def check(script: LessonScript) -> list[Turn]:
        turns = checked_turns(core_turns(script.turns), HOSTS)
        if (words := count_words(turns)) > ceiling:
            raise BadAnswerError(
                f"o roteiro veio com {words} palavras, acima de {ceiling}"
            )
        return turns

    return check


def checked_review(
    words: int, turns: int, max_words: int
) -> Callable[[ReviewedScript], tuple[list[Turn], list[str]]]:
    """The review may cut and correct, but it adds no turn, grows at most
    MAX_REVIEW_GROWTH, and never passes the lesson's own ceiling, so the lesson stays
    under 10 minutes."""
    ceiling = min(
        round(MAX_REVIEW_GROWTH * words), round(LONG_SCRIPT_RATIO * max_words)
    )

    def check(review: ReviewedScript) -> tuple[list[Turn], list[str]]:
        reviewed = checked_turns(core_turns(review.turns), HOSTS)
        if len(reviewed) > turns:
            raise BadAnswerError(
                f"a revisão veio com {len(reviewed)} falas, mais que as {turns} "
                "do roteiro"
            )
        if (reviewed_words := count_words(reviewed)) > ceiling:
            raise BadAnswerError(
                f"a revisão veio com {reviewed_words} palavras, acima de {ceiling}"
            )
        return reviewed, review.changes

    return check


def core_turns(turns: Sequence[LessonTurn]) -> list[Turn]:
    return [Turn(speaker=turn.speaker, text=turn.text) for turn in turns]


def script_material(
    topic: TopicToTry, pages: Sequence[LessonPage], catalog: Sequence[CatalogTopic]
) -> str:
    parts = []
    if topic.hook is not None:
        parts.append(story_line("Notícia de hoje que abre a aula", topic.hook))
    parts.append(course_lines(topic.topic, catalog))
    parts.append(pages_material(pages))
    return "\n\n".join(parts)


def review_material(
    topic: TopicToTry,
    pages: Sequence[LessonPage],
    turns: Sequence[Turn],
    catalog: Sequence[CatalogTopic],
) -> str:
    """The course's titles too, so the review can tell a real lesson from one the
    script made up."""
    parts = [pages_material(pages)]
    if topic.hook is not None:
        parts.append(story_line("Notícia que abriu a aula", topic.hook))
    parts.append(course_lines(topic.topic, catalog))
    lines = [f"{turn.speaker}: {turn.text}" for turn in turns]
    parts.append("Roteiro:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def pages_material(pages: Sequence[LessonPage]) -> str:
    blocks = [
        f"{number}. {one_line(page.title)} ({page.site}, "
        f"{page.published or 'sem data'})\n"
        f"Trecho: {one_line(page.excerpt)}\n"
        f"Texto:\n{page.text}"
        for number, page in enumerate(pages, start=1)
    ]
    return "Páginas:\n" + "\n\n".join(blocks)


def story_line(label: str, story: NewsStory) -> str:
    return f"{label}: {one_line(story.title)} ({one_line(story.angle)})"


def course_lines(topic: CatalogTopic, catalog: Sequence[CatalogTopic]) -> str:
    taught, upcoming = area_course(topic, catalog)
    return (
        f"Aulas já dadas nesta área, na ordem: {'; '.join(taught) or 'nenhuma'}\n"
        f"Próximos temas da área: {'; '.join(upcoming) or 'nenhum'}"
    )


def area_course(
    topic: CatalogTopic, catalog: Sequence[CatalogTopic]
) -> tuple[list[str], list[str]]:
    """The area's taught topics and its upcoming ones, in teaching order."""
    area = sorted(
        (other for other in catalog if other.area_id == topic.area_id),
        key=teaching_order,
    )
    taught = [other.title for other in area if other.taught_on is not None]
    upcoming = [
        other.title
        for other in area
        if other.active and other.taught_on is None and other.id != topic.id
    ]
    return taught, upcoming
