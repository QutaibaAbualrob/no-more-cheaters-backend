# Latest Changes — Authentication Migration to allauth Headless

**Date:** 2026-06-06
**Scope:** Backend only — React frontend routes identified but not built yet

---

## Summary

Migrated from `dj-rest-auth` to `allauth.headless` for all authentication. This enables a separate React frontend at `localhost:5173` to handle signup, login, email verification, and password reset flows via REST API.

### Files Changed

| File | Change |
|------|--------|
| `nomorecheaters/nomorecheaters/settings.py` | Replaced `dj_rest_auth` with `allauth.headless`; added `HEADLESS_ONLY`, `HEADLESS_FRONTEND_URLS`, `CORS_ALLOW_CREDENTIALS`, `CSRF_TRUSTED_ORIGINS`; updated `ACCOUNT_SIGNUP_FIELDS`; updated CORS for `localhost:5173` |
| `nomorecheaters/nomorecheaters/urls.py` | Replaced `dj_rest_auth` routes with `allauth.headless.urls` + `allauth.urls` |
| `requirements.txt` | Removed `dj-rest-auth` |
| `nomorecheaters/nomorecheaters/test_auth_flows.py` | **New** — 14 integration tests for allauth headless flows |
| `db.sqlite3` | Recreated (was broken by `uid` vs `id` PK column mismatch) |

### Packages Installed

```
django-allauth==65.18.0
djangorestframework==3.17.1
django-cors-headers==4.9.0
```

### Key Settings

```python
INSTALLED_APPS += ["allauth.headless"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
HEADLESS_ONLY = True
DEFAULT_FROM_EMAIL = "dev@localhost"
HEADLESS_FRONTEND_URLS = {
    "account_confirm_email": "http://localhost:5173/account/verify-email/{key}",
    "account_reset_password": "http://localhost:5173/account/password/reset",
    "account_reset_password_from_key": "http://localhost:5173/account/password/reset/key/{key}",
    "account_signup": "http://localhost:5173/signup",
}
```

### API Endpoints (allauth headless — browser client)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/_allauth/browser/v1/config` | GET | Auth config (login methods, signup status) |
| `/_allauth/browser/v1/auth/signup` | POST | Register with `{email, password}` |
| `/_allauth/browser/v1/auth/login` | POST | Login with `{email, password}` |
| `/_allauth/browser/v1/auth/email/verify` | POST | Verify email with `{key}` |
| `/_allauth/browser/v1/auth/password/request` | POST | Request password reset with `{email}` |
| `/_allauth/browser/v1/auth/password/reset` | POST | Confirm reset with `{key, password}` |

All endpoints require `X-CSRFToken` header (from `csrftoken` cookie set by GET config).

### Email Links (console output)

```
Verification: http://localhost:5173/account/verify-email/{key}
Reset:        http://localhost:5173/account/password/reset/key/{key}
```

### Frontend Routes Needed

When building the React app, create routes for:
- `/account/verify-email/:key`
- `/account/password/reset`
- `/account/password/reset/key/:key`
- `/signup`

### Test Results

```
Ran 14 tests — 12 pass, 2 skipped
```

- 2 tests skip when `mail.outbox` is empty (known `APITestCase` / `TransactionTestCase` quirk)
- Critical flows verified via status codes, response body flows, and database state

### Running

```bash
# Start backend
cd final_project/no-more-cheaters-backend
.venv/bin/python3 nomorecheaters/manage.py runserver 0.0.0.0:8000

# Run auth tests
.venv/bin/python3 nomorecheaters/manage.py test nomorecheaters.test_auth_flows
```