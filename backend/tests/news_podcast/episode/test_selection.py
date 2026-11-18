import asyncio
import json
from datetime import datetime

import httpx
import pytest
from pytest_django import Settings

from news_podcast.api.core.config import LOCAL_TZ
from news_podcast.api.core.llm import BadAnswerError, TextModels
from news_podcast.api.episode import prompts
from news_podcast.api.episode.feeds import format_for_selection
from news_podcast.api.episode.prompts import selection_template
from news_podcast.api.episode.run import episode_settings
from news_podcast.api.episode.schemas import (
    EpisodeOptions,
    NewsItem,
    SelectedStory,
    StorySelection,
)
from news_podcast.api.episode.selection import (
    checked_selection,
    select_stories,
    selection_values,
)
from tests.news_podcast.factories import SETTINGS, build_settings
from tests.news_podcast.fake_models import FakeChatModel, fake_factory

DEFAULT_OPTIONS = EpisodeOptions(12, 18, 1.0)
START = datetime(2026, 11, 6, 6, 0, tzinfo=LOCAL_TZ)
END = datetime(2026, 11, 7, 6, 0, tzinfo=LOCAL_TZ)


def news_item(item_id: str, title: str = "t") -> NewsItem:
    return NewsItem(
        id=item_id,
        outlet="g1",
        section="Política",
        title=title,
        summary="",
        link="",
        published_at=None,
    )


def every_placeholder(fields: tuple[str, ...]) -> str:
    return " ".join(f"{{{field}}}" for field in fields)


def test_the_selection_drops_unknown_ids_and_needs_a_valid_story() -> None:
    check = checked_selection({"3": news_item("3")}, max_stories=10)

    with pytest.raises(BadAnswerError, match="sem notícias válidas"):
        check(StorySelection(stories=[SelectedStory(title="x", item_ids=["42"])]))

    stories = check(
        StorySelection(
            stories=[
                SelectedStory(title="ok", item_ids=["[3]", "99", "3"]),
                SelectedStory(title="vazia", item_ids=["42"]),
            ]
        )
    )
    assert [story.title for story in stories] == ["ok"]
    assert stories[0].item_ids == ["3"]


def test_selected_story_accepts_numeric_ids() -> None:
    story = SelectedStory.model_validate({"title": "x", "item_ids": [12, 48]})

    assert story.item_ids == ["12", "48"]


def test_the_selection_fills_every_placeholder_but_not_the_headlines_braces() -> None:
    # A placeholder left without a value would raise KeyError.
    template = selection_template(
        every_placeholder(prompts.SELECTION_FIELDS) + " {{sem JSON}}"
    )

    system, human = template.format_messages(
        headlines='Ministro diz "{x}" e {y}',
        **selection_values(DEFAULT_OPTIONS, START, END),
    )

    assert system.content == "12 18 15 8 12 06/11 às 06:00 07/11 às 06:00 {sem JSON}"
    assert human.content == 'Ministro diz "{x}" e {y}'


def test_the_selection_step_returns_the_valid_stories(settings: Settings) -> None:
    settings.GEMINI_API_KEY = "test-key"
    model = SETTINGS["text_model"]
    items = [news_item("3", "Juros sobem"), news_item("7", "Chuva no Sul")]
    answer = {
        "stories": [
            {
                "title": "Juros",
                "category": "economia",
                "minutes": 2,
                "angle": "a",
                "item_ids": ["3"],
            },
            {
                "title": "Sem fonte",
                "category": "geral",
                "minutes": 1,
                "angle": "",
                "item_ids": ["99"],
            },
        ]
    }
    asked: list[str] = []
    built: dict[str, FakeChatModel] = {}

    async def scenario() -> list[SelectedStory]:
        async with httpx.AsyncClient() as client:
            text = TextModels(
                [model],
                [],
                client,
                fake_factory({model: [json.dumps(answer)]}, asked, built),
            )
            snapshot = episode_settings(build_settings())
            return await select_stories(text, snapshot, items, START, END)

    stories = asyncio.run(scenario())

    assert [story.title for story in stories] == ["Juros"]
    [messages] = built[model].received
    assert messages[1].content == format_for_selection(items, LOCAL_TZ)
