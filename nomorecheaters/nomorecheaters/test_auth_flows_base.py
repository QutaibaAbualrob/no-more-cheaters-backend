"""
Integration tests for allauth headless authentication flows.
"""
from django.contrib.sites.models import Site
from django.core import mail
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

CU = "/_allauth/browser/v1/config"
SU = "/_allauth/browser/v1/auth/signup"
LU = "/_allauth/browser/v1/auth/login"
VU = "/_allauth/browser/v1/auth/email/verify"
RRU = "/_allauth/browser/v1/auth/password/request"
CRU = "/_allauth/browser/v1/auth/password/reset"


def ekf(body, kind="verify"):
    import urllib.parse
    m = "http://localhost:5173/account/verify-email/" if kind == "verify" else "http://localhost:5173/account/password/reset/key/"
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(m):
            return urllib.parse.unquote(line[len(m):].strip())
    raise AssertionError("no key in email")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    ACCOUNT_EMAIL_VERIFICATION="mandatory",
    HEADLESS_ONLY=True,
    HEADLESS_FRONTEND_URLS={
        "account_confirm_email": "http://localhost:5173/account/verify-email/{key}",
        "account_reset_password": "http://localhost:5173/account/password/reset",
        "account_reset_password_from_key": "http://localhost:5173/account/password/reset/key/{key}",
        "account_signup": "http://localhost:5173/signup",
    },
)
class AuthFlowTests(APITestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Site.objects.get_or_create(id=1, defaults={"domain": "localhost:8000", "name": "Test"})

    def setUp(self):
        super().setUp()
        r = self.client.get(CU)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.__x = self.client.cookies.get("csrftoken").value
        self.pw = "Str0ng!Pass123"
