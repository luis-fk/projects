from collections.abc import Callable, Iterator
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponseBase
from django.test import Client

from political_culture.api.chatbot.routes import ChatBotApi
from political_culture.models import ChatHistory
from tests.api_tokens import (
    api_key,
    bearer,
    make_token,
    saved_person,
    user_token,
)

APP = "political-culture"
UPLOAD_ID = "2f6d9c1a-5b7e-4c3d-8a9f-0e1b2c3d4e5f"

Send = Callable[[Client, int, dict[str, str]], HttpResponseBase]


def get_history(
    client: Client, user_id: int, headers: dict[str, str]
) -> HttpResponseBase:
    return client.get(f"/api/political-culture/chat-history/{user_id}", headers=headers)


def send_message(
    client: Client, user_id: int, headers: dict[str, str]
) -> HttpResponseBase:
    return client.post(
        "/api/political-culture/chatbot/message",
        {"user_id": user_id, "text": "Olá"},
        content_type="application/json",
        headers=headers,
    )


def list_texts(
    client: Client, user_id: int, headers: dict[str, str]
) -> HttpResponseBase:
    return client.get("/api/political-culture/texts", headers=headers)


def select_text(
    client: Client, user_id: int, headers: dict[str, str]
) -> HttpResponseBase:
    return client.put(
        "/api/political-culture/texts/1/selection",
        {"selected": True},
        content_type="application/json",
        headers=headers,
    )


def add_text(client: Client, user_id: int, headers: dict[str, str]) -> HttpResponseBase:
    pdf = SimpleUploadedFile("texto.pdf", b"%PDF-1.4", content_type="application/pdf")
    return client.post(
        "/api/political-culture/texts",
        {"content": pdf, "upload_id": UPLOAD_ID},
        headers=headers,
    )


def get_help(client: Client, user_id: int, headers: dict[str, str]) -> HttpResponseBase:
    return client.get(
        "/api/political-culture/chatbot/help?language=en", headers=headers
    )


def find_user(client: Client, headers: dict[str, str]) -> HttpResponseBase:
    return client.get("/api/political-culture/users/ana", headers=headers)


ALL = [get_history, send_message, list_texts, add_text, select_text, get_help]
OWN_DATA = [get_history, send_message]


@pytest.mark.parametrize("send", ALL)
def test_requests_without_a_token_get_401(client: Client, send: Send) -> None:
    assert send(client, 7, {}).status_code == 401


@pytest.mark.parametrize("send", ALL)
def test_tokens_for_another_app_get_401(client: Client, send: Send) -> None:
    headers = bearer(user_token("plants", "7"))

    assert send(client, 7, headers).status_code == 401


@pytest.mark.parametrize("send", OWN_DATA)
def test_tokens_for_another_user_get_403(client: Client, send: Send) -> None:
    headers = bearer(user_token(APP, "7"))

    assert send(client, 8, headers).status_code == 403


@pytest.fixture
def app_data() -> Iterator[None]:
    """The chat history, the chatbot and the texts, without their database."""
    with (
        patch.object(ChatHistory.objects, "filter"),
        patch.object(ChatBotApi, "llm"),
        patch("political_culture.api.texts.routes.library_texts", return_value=[]),
        patch("political_culture.api.texts.routes.start_upload"),
        patch("political_culture.api.texts.routes.select_text"),
    ):
        yield


@pytest.mark.django_db
@pytest.mark.usefixtures("app_data")
@pytest.mark.parametrize(
    ("send", "status"),
    [
        (get_history, 200),
        (send_message, 200),
        (list_texts, 200),
        (add_text, 202),
        (select_text, 204),
        (get_help, 200),
    ],
)
def test_api_keys_act_as_their_user(client: Client, send: Send, status: int) -> None:
    assert send(client, 7, bearer(api_key(APP, 7))).status_code == status


@pytest.mark.django_db
@pytest.mark.parametrize("send", OWN_DATA)
def test_api_keys_for_another_user_get_403(client: Client, send: Send) -> None:
    assert send(client, 8, bearer(api_key(APP, 7))).status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("send", ALL)
def test_api_keys_for_another_app_get_401(client: Client, send: Send) -> None:
    assert send(client, 7, bearer(api_key("plants", 7))).status_code == 401


@pytest.mark.django_db
def test_the_login_lookup_without_a_login_token_gets_401(client: Client) -> None:
    assert find_user(client, {}).status_code == 401
    assert find_user(client, bearer(api_key(APP, 7))).status_code == 401


@pytest.mark.parametrize(
    ("app", "purpose"),
    [(APP, "user"), ("plants", "login")],
    ids=["user token", "another app's login token"],
)
def test_the_login_lookup_with_another_token_gets_401(
    client: Client, app: str, purpose: str
) -> None:
    headers = bearer(make_token(sub="7", app=app, purpose=purpose))

    assert find_user(client, headers).status_code == 401


@pytest.mark.access_check
@pytest.mark.django_db(databases=["default", "political_culture_db"])
@pytest.mark.parametrize(
    ("active", "status"),
    [(True, 200), (False, 401)],
    ids=["can use political culture", "political culture unticked"],
)
def test_only_people_who_can_use_political_culture_get_in(
    client: Client, active: bool, status: int
) -> None:
    user_id = saved_person(APP, active=active)

    response = get_history(client, int(user_id), bearer(user_token(APP, user_id)))

    assert response.status_code == status
