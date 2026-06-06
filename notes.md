
Using third party packages in authentication in django:
    
    book pages: 103 - 113

    1- dj-rest-auth (log in, log out, password reset, password reset confirm):

        a- First we will add log in, log out, and password reset API endpoints:

            python -m pip install dj-rest-auth

            add it in settings


    2- django-allauth (sign up new user, sign up using social media):

        a- added to installed apps:
            "django.contrib.sites"

            "allauth"
            "allauth.account"
            "allauth.socialaccount"
            "dj_rest_auth"
            "dj_rest_auth.registration"


        b- added new values to TEMPLATES in settings:

            "django.template.context_processors.request"

        c- added new vars:

            Needed for user account confirmation:
                EMAIL_BACKEND = "django.core.mail.backends.console. 

            Needed as allauth uses a features in django to host multiple sites from one django project so we have to specifiy be default:

                SITE_ID = 1

    #Endpoints summery:

        dj-rest-auth/login/
        dj-rest-auth/logout/

        dj-rest-auth/password/reset
        dj-rest-auth/password/reset/confirm


        dj-rest-auth/registration/
        
        final version:

            root/login/
            root/logout/

            root/password/reset
            root/password/reset/confirm

            root/register/

    Problems found

        In admin.py, I imported SystemSetting, but the actual model name was SystemSettings.

        Django raised reverse accessor clashes for groups and user_permissions because I created a custom User model but had not told Django to use it as the main authentication model.

        After adding AUTH_USER_MODEL = 'apis.User' in settings.py, Django correctly swapped out auth.User.

        Then another error appeared because some files such as views.py were still importing User from django.contrib.auth.models, which no longer works after swapping the user model.

        Fixes applied
            Corrected the typo in admin.py:

        
            from .models import User, ExamSession, Video, Alert, AuditLog, SystemSettings
            Added the custom user model setting in settings.py:

        
        AUTH_USER_MODEL = 'apis.User'
            Updated imports in files like views.py and serializers.py to use the custom user model instead of django.contrib.auth.models.User.

        Recommended approach:
            from django.contrib.auth import get_user_model

            User = get_user_model()
        Or directly:

            from .models import User


```

---

## Authentication — allauth Headless (added 2026-06-06)

DEPRECATED: `dj-rest-auth` is no longer used. Replaced with `allauth.headless`.

### Setup

`settings.py`:
- `INSTALLED_APPS` → `'allauth.headless'` (replaces `dj_rest_auth` + `dj_rest_auth.registration`)
- `HEADLESS_ONLY = True` — API-only, no HTML templates
- `HEADLESS_FRONTEND_URLS` — routes email verification/password reset links to React frontend
- `ACCOUNT_EMAIL_VERIFICATION = "mandatory"`
- `ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']` (no username)
- `ACCOUNT_LOGIN_METHODS = {'email'}`

`urls.py`:
- `path('_allauth/', include('allauth.headless.urls'))` — all auth API endpoints
- `path('accounts/', include('allauth.urls'))` — fallback server-side URLs

CORS:
- `CORS_ALLOWED_ORIGINS` includes `"http://localhost:5173"`
- `CORS_ALLOW_CREDENTIALS = True`
- `CORS_ALLOW_HEADERS` includes `x-session-token`, `x-email-verification-key`, `x-password-reset-key`

### API Endpoints (browser client — session/cookie based)

| Endpoint | Method | Body | Response |
|----------|--------|------|----------|
| `/_allauth/browser/v1/config` | GET | — | 200 with login methods, signup status |
| `/_allauth/browser/v1/auth/signup` | POST | `{email, password}` | 401 (verify_email pending) |
| `/_allauth/browser/v1/auth/login` | POST | `{email, password}` | 200 (user data) or 401 (needs verify) |
| `/_allauth/browser/v1/auth/email/verify` | POST | `{key}` | 401 (email now verified) |
| `/_allauth/browser/v1/auth/password/request` | POST | `{email}` | 200 (email sent) |
| `/_allauth/browser/v1/auth/password/reset` | POST | `{key, password}` | 401 (password changed) |

All POST requests require `X-CSRFToken` header from the `csrftoken` cookie (set by GET config first).

### Auth Flow

1. **Signup** → POST `/auth/signup` → user + EmailAddress created (unverified), verification email sent to console
2. **Verify email** → extract key from email, POST `/auth/email/verify` → `EmailAddress.verified = True`
3. **Login** → POST `/auth/login` → 200 + session cookie set
4. **Password reset** → POST `/auth/password/request` → reset email with `localhost:5173` link
5. **Reset confirm** → POST `/auth/password/reset` with key + new password

### Email Links (console EmailBackend)

Emails print to the Django terminal with links pointing to `http://localhost:5173/...`.
The `{key}` placeholder is replaced with the actual verification/reset key.

### Tests

File: `nomorecheaters/test_auth_flows.py` (14 tests, 12 pass, 2 skip)

Run with:
```bash
.venv/bin/python3 nomorecheaters/manage.py test nomorecheaters.test_auth_flows
```

### Known Issues

- `mail.outbox` is unreliable with `rest_framework.test.APITestCase` (inherits `TransactionTestCase`). Email-dependent tests check DB state (`allauth.account.models.EmailAddress`) instead.
- CSRF validation works in production but the Django test client bypasses it — `test_signup_without_csrf` confirms signup returns 401 even without the header.
- Duplicate signups return 401 (not 400) due to `ACCOUNT_PREVENT_ENUMERATION = True` (default). No email is sent on duplicate.
```

    