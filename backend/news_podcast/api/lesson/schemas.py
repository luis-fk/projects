"""The lesson's values, which its nodes read and pass on, and its answer schemas.
Gemini reads the field descriptions, so they are in Portuguese, like the prompts."""

from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from news_podcast.api.core.schemas import NewsStory


class CatalogTopic(NamedTuple):
    """A topic of the catalog as the run read it, with the São Paulo date of its
    latest sent lesson."""

    id: int
    area_id: int
    area: str
    area_position: int
    position: int
    title: str
    active: bool
    taught_on: date | None


class TopicToTry(NamedTuple):
    """A topic the lesson may teach, with the story that motivated it, if any."""

    topic: CatalogTopic
    hook: NewsStory | None


class TopicChoice(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    candidate: int = Field(
        description="O número do tema escolhido na lista de candidatos, ou 0 quando "
        "nenhum se liga claramente a uma notícia."
    )
    story: int = Field(
        description="O número da notícia a que o tema escolhido se liga, ou 0."
    )


@dataclass(frozen=True)
class LessonOptions:
    """The lesson settings the nodes read, with the speech speed from Settings."""

    min_minutes: int
    max_minutes: int
    speech_speed: float
    voice_model: str
    voice_style: str
    choice_prompt: str
    pick_prompt: str
    script_prompt: str
    review_prompt: str
