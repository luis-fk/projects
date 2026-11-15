"""The lesson's values, which its nodes read and pass on, and its answer schemas.
Gemini reads the field descriptions, so they are in Portuguese, like the prompts."""

from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from news_podcast.api.core.schemas import NewsStory
from news_podcast.api.lesson.config import MIN_LESSON_PAGES


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

    # Strict, so a JSON true never reads as 1.
    candidate: StrictInt = Field(
        description="O número do tema escolhido na lista de candidatos, ou 0 quando "
        "nenhum se liga claramente a uma notícia."
    )
    story: StrictInt = Field(
        description="O número da notícia a que o tema escolhido se liga, ou 0."
    )


class SearchResult(NamedTuple):
    """A search result that counts: a trusted host and enough text."""

    url: str
    title: str
    site: str
    published: str | None
    excerpt: str
    text: str


class LessonPage(NamedTuple):
    """A page the lesson rests on: the excerpt the pick judged it by, and its text
    from the start, cut to MAX_PAGE_CHARS, so the excerpt may lie past the cut."""

    title: str
    site: str
    url: str
    published: str | None
    excerpt: str
    text: str


class PagePick(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    pages: list[StrictInt] = Field(
        description="Os números das páginas escolhidas, da melhor para a pior; "
        "vazio quando nenhuma serve."
    )
    terms: str = Field(
        max_length=300,
        description=f"Outros termos de busca quando faltam {MIN_LESSON_PAGES} páginas "
        "boas e atuais; vazio quando as páginas escolhidas bastam.",
    )


class LessonTurn(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    speaker: str = Field(description="Quem fala: um dos dois apresentadores.")
    text: str = Field(description="O que a pessoa diz, escrito para ser ouvido.")


class LessonScript(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    turns: list[LessonTurn] = Field(description="As falas da aula, na ordem.")


class ReviewedScript(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    turns: list[LessonTurn] = Field(
        description="O roteiro revisado, fala por fala, na ordem."
    )
    changes: list[str] = Field(
        description="Cada mudança feita e por quê, uma frase por mudança; vazio "
        "quando nada mudou."
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
