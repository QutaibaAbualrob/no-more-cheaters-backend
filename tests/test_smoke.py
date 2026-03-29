import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_token_obtain_requires_user(api_client):
    url = reverse("token_obtain_pair")
    r = api_client.post(url, {"username": "nope", "password": "bad"}, format="json")
    assert r.status_code == 401
