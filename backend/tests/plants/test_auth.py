from collections.abc import Callable

import pytest
from django.http import HttpResponseBase
from django.test import Client

from tests.api_tokens import api_key, bearer, user_token

Send = Callable[[Client, int, dict[str, str]], HttpResponseBase]


def get_history(
    client: Client, user_id: int, headers: dict[str, str]
) -> HttpResponseBase:
    return client.get(f"/api/chat-history/{user_id}/", headers=headers)


def send_message(
    client: Client, user_id: int, headers: dict[str, str]
) -> HttpResponseBase:
    return client.post(
        "/api/chatbot/message",
        {"user_id": user_id, "message": "Olá"},
        content_type="application/json",
        headers=headers,
    )


def find_user(client: Client, headers: dict[str, str]) -> HttpResponseBase:
    return client.get("/api/plants/users/ana/", headers=headers)


@pytest.mark.parametrize("send", [get_history, send_message])
def test_requests_without_a_token_get_401(client: Client, send: Send) -> None:
    assert send(client, 7, {}).status_code == 401


@pytest.mark.parametrize("send", [get_history, send_message])
def test_tokens_for_another_user_get_403(client: Client, send: Send) -> None:
    headers = bearer(user_token("plants", "7"))

    assert send(client, 8, headers).status_code == 403


def test_auth_errors_use_the_api_error_format(client: Client) -> None:
    unauthenticated = client.get("/api/chat-history/7/")
    forbidden = client.get(
        "/api/chat-history/8/", headers=bearer(user_token("plants", "7"))
    )

    assert unauthenticated.json() == {"error": "Missing or invalid token."}
    assert unauthenticated["WWW-Authenticate"] == "Bearer"
    assert forbidden.json() == {"error": "The token belongs to another user."}


@pytest.mark.django_db
@pytest.mark.parametrize("send", [get_history, send_message])
def test_api_keys_for_another_user_get_403(client: Client, send: Send) -> None:
    assert send(client, 8, bearer(api_key("plants", 7))).status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("send", [get_history, send_message])
def test_api_keys_for_another_app_get_401(client: Client, send: Send) -> None:
    headers = bearer(api_key("political-culture", 7))

    assert send(client, 7, headers).status_code == 401


@pytest.mark.django_db
def test_the_login_lookup_without_a_login_token_gets_401(client: Client) -> None:
    assert find_user(client, {}).status_code == 401
    assert find_user(client, bearer(api_key("plants", 7))).status_code == 401


def test_the_login_lookup_with_a_user_token_gets_401(client: Client) -> None:
    assert find_user(client, bearer(user_token("plants"))).status_code == 401
