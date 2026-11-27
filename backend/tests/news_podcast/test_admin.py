from datetime import UTC, datetime
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import models
from django.forms.models import model_to_dict
from django.test import Client
from pytest_django import DjangoAssertNumQueries

from news_podcast.models import (
    LESSON_PROMPT_FIELDS,
    Episode,
    LessonSettings,
    PodcastSettings,
    placeholder_error,
)
from tests.news_podcast.factories import (
    create_area,
    create_lesson_row,
    create_lesson_settings,
    create_settings,
    create_topic,
)

SETTINGS_URL = "/admin/news_podcast/podcastsettings/"
EPISODES_URL = "/admin/news_podcast/episode/"
AREAS_URL = "/admin/news_podcast/area/"
TOPICS_URL = "/admin/news_podcast/topic/"
LESSON_URL = "/admin/news_podcast/lessonsettings/"
DATABASES = pytest.mark.django_db(databases=["default", "news_podcast_db"])


def form_data(row: models.Model, **changes: Any) -> dict[str, Any]:
    data = {**model_to_dict(row), **changes}
    del data["id"]
    return data


@DATABASES
def test_the_settings_page_asks_for_the_seed_then_opens_the_row(
    admin_client: Client,
) -> None:
    missing = admin_client.get(SETTINGS_URL)
    row = create_settings()
    found = admin_client.get(SETTINGS_URL)

    assert missing.status_code == 200
    assert "seed_news_podcast" in missing.content.decode()
    assert list(PodcastSettings.objects.all()) == [row]
    assert found.status_code == 302
    assert found["Location"] == f"{SETTINGS_URL}{row.pk}/change/"


@DATABASES
def test_the_admin_index_never_queries_the_podcast_database(
    admin_client: Client, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    with django_assert_num_queries(0, using="news_podcast_db"):
        response = admin_client.get("/admin/")

    assert response.status_code == 200
    page = response.content.decode()
    assert f'href="{AREAS_URL}"' in page
    assert f'href="{TOPICS_URL}"' in page


@DATABASES
@pytest.mark.parametrize("with_row", [True, False])
def test_the_settings_page_needs_the_permission_before_anything_else(
    client: Client, with_row: bool
) -> None:
    if with_row:
        create_settings()
    staff = get_user_model().objects.create_user(
        username="ana", password="senha", is_staff=True
    )
    client.force_login(staff)

    response = client.get(SETTINGS_URL)

    assert response.status_code == 403
    assert list(get_messages(response.wsgi_request)) == []


@DATABASES
def test_the_settings_rows_can_be_neither_added_nor_deleted(
    admin_client: Client,
) -> None:
    rows: dict[str, models.Model] = {
        SETTINGS_URL: create_settings(),
        LESSON_URL: create_lesson_settings(),
    }

    for url, row in rows.items():
        assert admin_client.get(f"{url}add/").status_code == 403
        assert admin_client.get(f"{url}{row.pk}/delete/").status_code == 403


@DATABASES
def test_the_settings_form_refuses_an_unknown_placeholder_and_saves_a_change(
    admin_client: Client,
) -> None:
    row = create_settings()
    url = f"{SETTINGS_URL}{row.pk}/change/"
    before = row.script_prompt

    refused = admin_client.post(url, form_data(row, script_prompt="Oi, {ouvinte}!"))

    errors = refused.context["adminform"].form.errors
    assert "{ouvinte}" in errors["script_prompt"][0]
    row.refresh_from_db()
    assert row.script_prompt == before

    changes = {"paused": True, "max_minutes": 15}
    second = {"second_fallback_model": "gemini-3.7-flash"}
    saved = admin_client.post(url, form_data(row, **changes, **second))

    assert saved.status_code == 302
    row.refresh_from_db()
    assert row.paused
    assert row.max_minutes == 15
    assert row.second_fallback_model == "gemini-3.7-flash"


@DATABASES
def test_the_history_shows_each_run_and_the_total_cost(admin_client: Client) -> None:
    Episode.objects.create(
        started_at=datetime(2026, 11, 9, 9, 10, tzinfo=UTC),
        status=Episode.Status.SENT,
        duration_seconds=900.4,
        script_words=2550,
        cost_usd=0.10,
    )
    Episode.objects.create(
        started_at=datetime(2026, 11, 8, 9, 10, tzinfo=UTC),
        status=Episode.Status.FAILED,
        cost_usd=0.08,
    )

    page = admin_client.get(EPISODES_URL).content.decode()

    assert "09/11/2026 06:10" in page
    assert '<td class="field-length">15:00</td>' in page
    assert '<td class="field-pace">170</td>' in page
    assert '<td class="field-pace">-</td>' in page
    # The cost column keeps "R$" with the number; the total uses a normal space.
    assert "R$\N{NO-BREAK SPACE}0,55" in page
    assert "R$\N{NO-BREAK SPACE}0,44" in page
    assert "<strong>R$ 0,99</strong>" in page
    assert admin_client.get(f"{EPISODES_URL}add/").status_code == 403


@DATABASES
def test_the_topics_list_shows_when_a_sent_lesson_taught_each_topic(
    admin_client: Client,
) -> None:
    area = create_area(name="O Congresso e as leis")
    taught = create_topic(area, title="Como o Congresso derruba um veto", position=11)
    tried = create_topic(area, title="Sanção e veto", position=10)
    create_topic(area, title="Impeachment", position=22)
    create_lesson_row(taught, started_at=datetime(2026, 11, 13, 1, 30, tzinfo=UTC))
    create_lesson_row(tried, status=Episode.Status.FAILED)
    create_lesson_row(tried, status=Episode.Status.NOT_SENT)
    create_lesson_row(tried, kind=Episode.Kind.NEWS)

    page = admin_client.get(TOPICS_URL).content.decode()

    # 01:30 UTC on the 13th is still the 12th in São Paulo.
    assert page.count('<td class="field-taught">12/11/2026</td>') == 1
    assert page.count('<td class="field-taught">-</td>') == 2
    for column in ["title", "area", "position", "active", "taught"]:
        assert f"column-{column}" in page
    assert "Taught on (Brasília)" in page


@DATABASES
def test_the_kind_filter_lists_the_lessons_and_adds_up_their_cost(
    admin_client: Client,
) -> None:
    Episode.objects.create(
        started_at=datetime(2026, 11, 13, 9, 40, tzinfo=UTC),
        status=Episode.Status.SENT,
        cost_usd=0.10,
    )
    create_lesson_row(
        started_at=datetime(2026, 11, 13, 10, 0, tzinfo=UTC), cost_usd=0.20
    )
    create_lesson_row(
        started_at=datetime(2026, 11, 15, 10, 0, tzinfo=UTC), cost_usd=0.30
    )

    response = admin_client.get(EPISODES_URL, {"kind__exact": "lesson"})
    page = response.content.decode()

    filters = [spec.title for spec in response.context["cl"].filter_specs]
    assert filters == ["kind", "status"]
    assert page.count('<td class="field-kind">Lesson</td>') == 2
    assert '<td class="field-kind">News</td>' not in page
    assert "<strong>R$ 2,75</strong>" in page


@DATABASES
def test_a_lesson_row_shows_its_topic_story_and_pages_as_text(
    admin_client: Client,
) -> None:
    topic = create_topic(create_area(), title="Como o Congresso derruba um veto")
    lesson = create_lesson_row(
        topic,
        story_title="Congresso derruba veto ao <marco> & cia",
        pages=[
            {
                "title": "Entenda o veto & a sessão",
                "url": "https://www12.senado.leg.br/veto?a=1&b=2",
            },
            {"title": "Vetos", "url": "https://www.camara.leg.br/vetos"},
            {"url": "javascript:alert(1)"},
            {"title": "Sem endereço"},
        ],
    )

    response = admin_client.get(f"{EPISODES_URL}{lesson.pk}/change/")

    page = response.content.decode()
    assert response.status_code == 200
    assert '<div class="readonly">Lesson</div>' in page
    assert "Como o Congresso derruba um veto" in page
    assert "Congresso derruba veto ao &lt;marco&gt; &amp; cia" in page
    assert (
        "Entenda o veto &amp; a sessão: https://www12.senado.leg.br/veto?a=1&amp;b=2"
    ) in page
    assert "Vetos: https://www.camara.leg.br/vetos" in page
    assert ": javascript:alert(1)" in page
    assert "Sem endereço: " in page
    assert 'href="javascript:' not in page


@DATABASES
def test_the_lesson_settings_page_asks_for_the_seed_then_opens_the_row(
    admin_client: Client,
) -> None:
    missing = admin_client.get(LESSON_URL)
    row = create_lesson_settings()
    found = admin_client.get(LESSON_URL)

    assert missing.status_code == 200
    assert "configurações da aula" in missing.content.decode()
    assert "seed_news_podcast" in missing.content.decode()
    assert list(LessonSettings.objects.all()) == [row]
    assert found.status_code == 302
    assert found["Location"] == f"{LESSON_URL}{row.pk}/change/"


@DATABASES
@pytest.mark.parametrize("with_row", [True, False])
def test_the_lesson_page_needs_the_permission_before_anything_else(
    client: Client, with_row: bool
) -> None:
    if with_row:
        create_lesson_settings()
    staff = get_user_model().objects.create_user(
        username="ana", password="senha", is_staff=True
    )
    client.force_login(staff)

    response = client.get(LESSON_URL)

    assert response.status_code == 403
    assert list(get_messages(response.wsgi_request)) == []


@DATABASES
def test_the_lesson_form_saves_a_blank_cadence_as_none(admin_client: Client) -> None:
    row = create_lesson_settings(every_days=5)
    url = f"{LESSON_URL}{row.pk}/change/"

    page = admin_client.get(url).content.decode()
    response = admin_client.post(url, form_data(row, every_days=""))

    # First in the Lesson section, before the minutes.
    assert -1 < page.find("A lesson every (days)") < page.find("Minimum minutes")
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.every_days is None


@DATABASES
def test_the_lesson_form_shows_the_placeholder_message_and_saves_a_full_prompt(
    admin_client: Client,
) -> None:
    row = create_lesson_settings()
    url = f"{LESSON_URL}{row.pk}/change/"
    fields = LESSON_PROMPT_FIELDS["review_prompt"]
    full = " ".join(f"{{{name}}}" for name in fields) + " {{literal}}"

    refused = admin_client.post(url, form_data(row, review_prompt="Oi, {ouvinte}!"))
    saved = admin_client.post(url, form_data(row, review_prompt=full))

    errors = refused.context["adminform"].form.errors["review_prompt"]
    assert errors == [placeholder_error("Oi, {ouvinte}!", fields)]
    assert saved.status_code == 302
    row.refresh_from_db()
    assert row.review_prompt == full
