"""The lesson's pages: a search on the trusted sites, the pick among the results, and
the walk to the next topic when one lacks pages."""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any, Literal, NamedTuple
from urllib.parse import urlsplit

import httpx
from tavily import errors as tavily_errors

from news_podcast.api.lesson.config import (
    LESSON_SOURCE_DOMAINS,
    MAX_LESSON_PAGES,
    MAX_PAGE_CHARS,
    MAX_SEARCHES,
    MAX_TOPICS_PER_LESSON,
    MIN_LESSON_PAGES,
    MIN_PAGE_CHARS,
    SEARCH_RESULTS,
)
from news_podcast.api.lesson.messages import skipped_warning
from news_podcast.api.lesson.prompts import lesson_date, material_template
from news_podcast.api.lesson.schemas import (
    LessonPage,
    PagePick,
    SearchResult,
    TopicToTry,
)
from news_podcast.api.lesson.state import (
    LessonContext,
    LessonError,
    LessonState,
    SearchClient,
    topic_start,
)

logger = logging.getLogger(__name__)

# Tavily's own TimeoutError isn't the builtin, so it can't pass for the lesson's limit.
SEARCH_FAILURES = (
    tavily_errors.MissingAPIKeyError,
    tavily_errors.InvalidAPIKeyError,
    tavily_errors.UsageLimitExceededError,
    tavily_errors.ForbiddenError,
    tavily_errors.BadRequestError,
    tavily_errors.TimeoutError,
    httpx.HTTPError,
)


class SearchError(LessonError):
    """The search failed, so no other topic is searched."""


class Pick(NamedTuple):
    pages: list[SearchResult]
    terms: str


async def search(state: LessonState, context: LessonContext) -> LessonState:
    """One search for the current topic: its title, or the pick's other terms. The
    results that count join the earlier ones, each URL once."""
    tried = current(state)
    query = state.get("terms") or tried.topic.title
    found = await run_search(context.search, query)
    counted = [result for raw in found if (result := counted_result(raw))]
    results = merged(state.get("results", []), counted)
    logger.info(f"Search for {query!r}: {len(counted)} of {len(found)} results count")
    return {"results": results, "searches": state.get("searches", 0) + 1, "terms": ""}


async def run_search(client: SearchClient | None, query: str) -> list[Any]:
    if client is None:
        raise SearchError("TAVILY_API_KEY não configurada")
    try:
        response = await client.search(
            query,
            search_depth="advanced",
            max_results=SEARCH_RESULTS,
            include_domains=list(LESSON_SOURCE_DOMAINS),
            include_raw_content=True,
            include_published_date=True,
        )
    except SEARCH_FAILURES as error:
        raise SearchError(
            f"busca do Tavily falhou ({type(error).__name__}): {error}"
        ) from error
    return list(response.get("results") or [])


def counted_result(raw: Mapping[str, Any]) -> SearchResult | None:
    """The result, when its host is a trusted site or a subdomain of one and its text
    is long enough; Tavily's filter only narrows the search."""
    url = raw.get("url") or ""
    site = (urlsplit(url).hostname or "").lower()
    trusted = any(
        site == domain or site.endswith(f".{domain}")
        for domain in LESSON_SOURCE_DOMAINS
    )
    text = raw.get("raw_content")
    if not trusted or not isinstance(text, str) or len(text) < MIN_PAGE_CHARS:
        return None
    return SearchResult(
        url=url,
        title=raw.get("title") or url,
        site=site,
        published=published_day(raw.get("published_date")),
        excerpt=raw.get("content") or "",
        text=text,
    )


def published_day(value: Any) -> str | None:
    """Tavily's date as YYYY-MM-DD. It comes as an HTTP date, such as "Thu, 30 Apr
    2026 18:53:56 GMT"; anything else that can't be read is kept as it came."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return value


def merged(
    earlier: Sequence[SearchResult], new: Sequence[SearchResult]
) -> list[SearchResult]:
    """The earlier results, then the new ones, each URL once."""
    results = list(earlier)
    seen = {result.url for result in results}
    for result in new:
        if result.url not in seen:
            seen.add(result.url)
            results.append(result)
    return results


async def pick_pages(state: LessonState, context: LessonContext) -> LessonState:
    """Up to MAX_LESSON_PAGES pages, best first, and maybe other search terms. Asked
    even with no result, so it can propose the terms."""
    tried = current(state)
    results = state.get("results", [])
    pick = await context.text.ask(
        material_template(context.options.pick_prompt),
        {
            "material": pick_material(results),
            "today": lesson_date(context.today),
            "topic": tried.topic.title,
            "area": tried.topic.area,
            "max_pages": MAX_LESSON_PAGES,
        },
        step="pick",
        schema=PagePick,
        check=checked_pick(results),
    )
    titles = ", ".join(page.title for page in pick.pages)
    logger.info(f"Picked for {tried.topic.title}: {titles or 'nothing'}")
    return {"picked": pick.pages, "terms": pick.terms}


def pick_material(results: Sequence[SearchResult]) -> str:
    lines = ["Resultados da busca:"]
    if not results:
        lines.append("Nenhum resultado dos sites confiáveis.")
    lines += [
        f"{number}. {result.title} ({result.site}, {result.published or 'sem data'}): "
        f"{result.excerpt}"
        for number, result in enumerate(results, start=1)
    ]
    return "\n".join(lines)


def checked_pick(results: Sequence[SearchResult]) -> Callable[[PagePick], Pick]:
    """Unknown numbers and repeats are dropped and the first MAX_LESSON_PAGES kept.
    Never a bad answer: too few pages only moves the lesson on."""

    def check(answer: PagePick) -> Pick:
        numbers = [n for n in dict.fromkeys(answer.pages) if 1 <= n <= len(results)]
        pages = [results[number - 1] for number in numbers[:MAX_LESSON_PAGES]]
        return Pick(pages, answer.terms.strip())

    return check


def after_pick(
    state: LessonState,
) -> Literal["other_terms", "enough_pages", "too_few_pages"]:
    """Other terms bring one more search, up to MAX_SEARCHES; then the pages decide."""
    if state.get("terms") and state.get("searches", 0) < MAX_SEARCHES:
        return "other_terms"
    if len(state.get("picked", [])) >= MIN_LESSON_PAGES:
        return "enough_pages"
    return "too_few_pages"


async def next_topic(state: LessonState, context: LessonContext) -> LessonState:
    """Gives the topic up and starts the next. After MAX_TOPICS_PER_LESSON topics,
    the lesson fails, listing them; LangGraph drops this node's writes then, so the
    list is built here."""
    index = state["topic_index"]
    topics = state["topics"]
    skipped = [*state.get("skipped", []), topics[index].topic.title]
    if len(skipped) >= MAX_TOPICS_PER_LESSON or index + 1 >= len(topics):
        raise LessonError(
            f"Nenhum tema teve pelo menos {MIN_LESSON_PAGES} páginas boas: "
            f"{'; '.join(skipped)}."
        )
    logger.info(f"Lesson topic skipped for lack of pages: {skipped[-1]}")
    return {**topic_start(index + 1), "skipped": skipped}


async def keep_pages(state: LessonState, context: LessonContext) -> LessonState:
    """Settles the topic and its pages, and warns once about a topic skipped on the
    way."""
    pages = [
        LessonPage(
            title=result.title,
            site=result.site,
            url=result.url,
            published=result.published,
            text=result.text[:MAX_PAGE_CHARS],
        )
        for result in state.get("picked", [])
    ]
    if skipped := state.get("skipped"):
        await context.warn(skipped_warning(skipped))
    return {"topic": current(state), "pages": pages}


def current(state: LessonState) -> TopicToTry:
    return state["topics"][state["topic_index"]]
