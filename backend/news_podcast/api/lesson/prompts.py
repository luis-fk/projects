"""Each lesson prompt's placeholders and its template. The prompts live in the lesson
settings row (the admin), which starts from the seed. Single values are placeholders;
lists go in the human message."""

from datetime import date

from langchain_core.prompts import ChatPromptTemplate

from news_podcast.api.core.messages import long_date

CHOICE_FIELDS = ("today",)
PICK_FIELDS = ("today", "topic", "area", "max_pages")
SCRIPT_FIELDS = (
    "host_a",
    "host_b",
    "today",
    "topic",
    "area",
    "min_words",
    "max_words",
    "news_sent",
)
REVIEW_FIELDS = ("today", "topic")


def material_template(prompt: str) -> ChatPromptTemplate:
    """The saved prompt is the system message. The step's material goes in as a
    variable, so its braces are never read as placeholders."""
    return ChatPromptTemplate.from_messages(
        [("system", prompt), ("human", "{material}")]
    )


def lesson_date(day: date) -> str:
    """Today with its year, since the prompts weigh how recent a page is."""
    return f"{long_date(day)} de {day.year}"
