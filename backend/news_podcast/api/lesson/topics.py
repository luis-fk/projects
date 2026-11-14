import logging
from collections.abc import Callable, Sequence
from datetime import date

from news_podcast.api.core.llm import TextError
from news_podcast.api.core.schemas import NewsStory
from news_podcast.api.lesson.prompts import lesson_date, material_template
from news_podcast.api.lesson.schemas import CatalogTopic, TopicChoice, TopicToTry
from news_podcast.api.lesson.state import LessonContext, LessonError, LessonState

logger = logging.getLogger(__name__)


def teaching_order(topic: CatalogTopic) -> tuple[int, int, int, int]:
    return (topic.area_position, topic.area_id, topic.position, topic.id)


def candidates(catalog: Sequence[CatalogTopic]) -> list[CatalogTopic]:
    """Per area, the first active topic not taught yet, in teaching order. The areas
    come in their own order."""
    offered: dict[int, CatalogTopic] = {}
    for topic in sorted(catalog, key=teaching_order):
        if topic.active and topic.taught_on is None:
            offered.setdefault(topic.area_id, topic)
    return list(offered.values())


def fallback_order(
    offered: Sequence[CatalogTopic], catalog: Sequence[CatalogTopic]
) -> list[CatalogTopic]:
    """The areas never taught first, in area order, then the one taught least
    recently, with ties in area order."""
    last_taught: dict[int, date] = {}
    for topic in catalog:
        if topic.taught_on is not None:
            previous = last_taught.get(topic.area_id, topic.taught_on)
            last_taught[topic.area_id] = max(previous, topic.taught_on)
    return sorted(
        offered,
        key=lambda topic: (
            topic.area_id in last_taught,
            last_taught.get(topic.area_id, date.min),
            topic.area_position,
            topic.area_id,
        ),
    )


async def choose_topic(state: LessonState, context: LessonContext) -> LessonState:
    """The topics to try, in order: the candidate the day's news calls for, if any,
    with its story as the hook, then the rest in fallback order."""
    catalog = context.catalog
    offered = candidates(catalog)
    if not offered:
        if not catalog:
            raise LessonError("O catálogo de temas está vazio: rode seed_news_podcast.")
        raise LessonError(
            "Nenhum tema disponível: todos foram dados ou estão desativados; "
            "cadastre novos temas no admin."
        )
    topics = [TopicToTry(topic, None) for topic in fallback_order(offered, catalog)]
    chosen = await choice(context, offered, state.get("stories", []))
    if chosen is not None:
        topic, story = chosen
        rest = [tried for tried in topics if tried.topic != topic]
        topics = [TopicToTry(topic, story), *rest]
        logger.info(f"Lesson topic: {topic.title}, tied to the story {story.title}")
    else:
        logger.info(f"Lesson topic, in the course's order: {topics[0].topic.title}")
    return {"topics": topics, "topic_index": 0, "skipped": []}


async def choice(
    context: LessonContext,
    offered: Sequence[CatalogTopic],
    stories: Sequence[NewsStory],
) -> tuple[CatalogTopic, NewsStory] | None:
    """Asked once, with no waits, since the fallback order is there: the waits stay
    for the later steps."""
    if not stories:
        return None
    try:
        return await context.text.ask(
            material_template(context.options.choice_prompt),
            {
                "material": choice_material(offered, stories),
                "today": lesson_date(context.today),
            },
            step="choice",
            schema=TopicChoice,
            check=checked_choice(offered, stories),
            wait=False,
        )
    except TextError as error:
        logger.warning(f"The topic choice failed, so the course's order goes: {error}")
        return None


def choice_material(
    offered: Sequence[CatalogTopic], stories: Sequence[NewsStory]
) -> str:
    lines = ["Temas candidatos:"]
    lines += [
        f"{number}. {topic.title} ({topic.area})"
        for number, topic in enumerate(offered, start=1)
    ]
    lines += ["", "Notícias de hoje, da mais importante para a menos importante:"]
    lines += [
        f"{number}. {story.title} ({story.category}): {story.angle}"
        for number, story in enumerate(stories, start=1)
    ]
    return "\n".join(lines)


def checked_choice(
    offered: Sequence[CatalogTopic], stories: Sequence[NewsStory]
) -> Callable[[TopicChoice], tuple[CatalogTopic, NewsStory] | None]:
    """A candidate counts only with a known story. Anything else is none, never a bad
    answer, so the choice isn't asked again."""

    def check(answer: TopicChoice) -> tuple[CatalogTopic, NewsStory] | None:
        known_topic = 1 <= answer.candidate <= len(offered)
        known_story = 1 <= answer.story <= len(stories)
        if known_topic and known_story:
            return offered[answer.candidate - 1], stories[answer.story - 1]
        return None

    return check
