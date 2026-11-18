import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from cfflch.models import Users

DATABASES = pytest.mark.django_db(databases=["default", "cfflch_db"])
USERS_URL = "/admin/cfflch/users/"


@DATABASES
def test_the_users_list_finds_a_person_by_name(admin_client: Client) -> None:
    for name in ["maria", "joana"]:
        person = get_user_model().objects.create_user(username=name)
        Users.objects.create(auth_user_id=person.pk)

    # A pasted name can bring spaces around it.
    page = admin_client.get(USERS_URL, {"q": " mar "}).content.decode()

    assert "maria" in page
    assert "joana" not in page


@DATABASES
def test_the_users_list_shows_and_filters_who_is_blocked(admin_client: Client) -> None:
    for name, active in [("maria", True), ("joana", False)]:
        person = get_user_model().objects.create_user(username=name)
        Users.objects.create(auth_user_id=person.pk, active=active)

    page = admin_client.get(USERS_URL, {"active__exact": "0"}).content.decode()

    assert "column-active" in page
    assert 'href="?active__exact=0"' in page
    assert "joana" in page
    assert "maria" not in page
