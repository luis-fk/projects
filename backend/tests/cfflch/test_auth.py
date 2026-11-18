from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, patch

import pytest
from django.http import HttpResponseBase
from django.test import Client

from cfflch.api.admission_status.routes import AdmissionStatusApi
from cfflch.models import ClassRoom
from tests.api_tokens import (
    api_key,
    bearer,
    login_token,
    saved_person,
    user_token,
)

Send = Callable[[Client, dict[str, str]], HttpResponseBase]


def list_class_rooms(client: Client, headers: dict[str, str]) -> HttpResponseBase:
    return client.get("/api/cfflch/class-rooms/", headers=headers)


def list_results(client: Client, headers: dict[str, str]) -> HttpResponseBase:
    return client.get("/api/cfflch/admission-results/", {"year": 2025}, headers=headers)


def update_result(client: Client, headers: dict[str, str]) -> HttpResponseBase:
    return client.patch(
        "/api/cfflch/admission-results/1/",
        {"approved": True},
        content_type="application/json",
        headers=headers,
    )


def delete_result(client: Client, headers: dict[str, str]) -> HttpResponseBase:
    return client.delete("/api/cfflch/admission-results/1/", headers=headers)


def start_search(client: Client, headers: dict[str, str]) -> HttpResponseBase:
    return client.post(
        "/api/cfflch/admission-status/",
        {"names": ["Ana Souza"], "year": 2025, "request_id": "abc"},
        content_type="application/json",
        headers=headers,
    )


def find_user(client: Client, headers: dict[str, str]) -> HttpResponseBase:
    return client.get("/api/cfflch/users/ana", headers=headers)


REQUESTS = [list_class_rooms, list_results, update_result, delete_result, start_search]
# A DRF view and the async view, with what each answers a cfflch user under the
# empty_database fixture; the other DRF views make the same require_user_token call.
STATUSES = [(list_class_rooms, 200), (start_search, 202)]


@pytest.fixture
def empty_database() -> Iterator[None]:
    """No class rooms, and searches that end right away."""
    with (
        patch.object(ClassRoom.objects, "all", return_value=[]),
        patch.object(AdmissionStatusApi, "_process_admission_task", AsyncMock()),
    ):
        yield


@pytest.mark.parametrize("send", REQUESTS)
def test_requests_without_a_cfflch_user_token_get_401(
    client: Client, send: Send
) -> None:
    assert send(client, {}).status_code == 401
    assert send(client, bearer(user_token("plants"))).status_code == 401


@pytest.mark.django_db
@pytest.mark.parametrize("send", REQUESTS)
def test_api_keys_for_another_app_get_401(client: Client, send: Send) -> None:
    assert send(client, bearer(api_key("plants"))).status_code == 401


@pytest.mark.django_db
@pytest.mark.usefixtures("empty_database")
@pytest.mark.parametrize(("send", "status"), STATUSES)
def test_any_cfflch_api_key_gets_in(client: Client, send: Send, status: int) -> None:
    """start_search is an async view: it looks the key up through sync_to_async."""
    assert send(client, bearer(api_key("cfflch", 8))).status_code == status


@pytest.mark.django_db
def test_the_login_lookup_without_a_cfflch_login_token_gets_401(client: Client) -> None:
    assert find_user(client, {}).status_code == 401
    assert find_user(client, bearer(user_token("cfflch"))).status_code == 401
    assert find_user(client, bearer(login_token("plants"))).status_code == 401
    assert find_user(client, bearer(api_key("cfflch"))).status_code == 401


@pytest.mark.access_check
@pytest.mark.django_db(databases=["default", "cfflch_db"])
@pytest.mark.usefixtures("empty_database")
@pytest.mark.parametrize(("send", "status"), STATUSES)
@pytest.mark.parametrize(
    "active", [True, False], ids=["can use cfflch", "cfflch unticked"]
)
def test_only_people_who_can_use_cfflch_get_in(
    client: Client, send: Send, status: int, active: bool
) -> None:
    """start_search is an async view: the check runs through sync_to_async."""
    user_id = saved_person("cfflch", active=active)
    expected = status if active else 401

    assert send(client, bearer(user_token("cfflch", user_id))).status_code == expected
