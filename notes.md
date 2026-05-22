# No More Cheaters - Project Notes

Last revised: 2026-05-22

These notes summarize the current backend/frontend integration work in the
`Senior Project` folder. Implementation files were not changed during this
documentation pass.

## Authentication Packages

The project uses third-party Django authentication packages:

1. `dj-rest-auth`

    Used for login, logout, password reset, password reset confirm, and user
    account endpoints.

    Install command:

        python -m pip install dj-rest-auth

2. `django-allauth`

    Used for registration and account management support.

    Required apps added to `INSTALLED_APPS`:

        django.contrib.sites
        allauth
        allauth.account
        allauth.socialaccount
        dj_rest_auth
        dj_rest_auth.registration

    Required template context processor:

        django.template.context_processors.request

    Required settings:

        EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
        SITE_ID = 1

## Authentication Endpoints

Current frontend authentication calls target the backend's mounted
`dj-rest-auth` URLs:

    /api/dj-rest-auth/login/
    /api/dj-rest-auth/logout/
    /api/dj-rest-auth/user/
    /api/dj-rest-auth/password/change/
    /api/dj-rest-auth/password/reset/
    /api/dj-rest-auth/password/reset/confirm/
    /api/dj-rest-auth/registration/

## Custom User Model

The project uses `apis.User` as the Django authentication model.

Important setting:

    AUTH_USER_MODEL = 'apis.User'

Problems previously fixed:

    admin.py imported SystemSetting, but the actual model name was
    SystemSettings.

    The custom User model caused reverse accessor clashes for groups and
    user_permissions until AUTH_USER_MODEL was configured.

    Some files still imported User from django.contrib.auth.models after the
    custom user model was enabled.

Recommended import pattern:

    from django.contrib.auth import get_user_model

    User = get_user_model()

The `User.role` field now defaults to `INSTRUCTOR`, applied by migration:

    0004_alter_user_role.py

## User Preferences

`UserPreferences` stores per-user application preferences separately from the
custom `User` model.

Why it is separate:

    User is for account identity and authentication fields.

    UserPreferences is for app settings that may grow over time, such as
    notifications, language, timezone, theme, metadata, and user-specific AI
    threshold overrides.

Model defaults:

    email_notifications = True
    dashboard_alerts = True
    preferred_language = 'en'
    timezone = 'UTC'
    theme = 'SYSTEM'
    metadata = {}

Endpoint:

    GET /api/me/preferences/
    PATCH /api/me/preferences/

Behavior:

    GET lazily creates default preferences for the authenticated user.

    PATCH updates only the authenticated user's preference row.

    The serializers do not expose a writable user field, so clients cannot
    move preferences to another account.

Migration:

    0003_userpreferences.py

## Backend API Added For Frontend Integration

The backend now exposes the app workflow endpoints used by the frontend:

    GET  /api/videos/
    POST /api/videos/upload/
    GET  /api/videos/<uuid:pk>/
    POST /api/videos/<uuid:pk>/analyze/
    GET  /api/history/

    GET  /api/dashboard/stats/
    GET  /api/dashboard/activity/

    GET   /api/thresholds/
    PATCH /api/thresholds/me/
    PATCH /api/thresholds/global/

    GET /api/system/logs/
    GET /api/system/metrics/

    GET /api/<uuid:pk>/activity/

Important behavior:

    Video upload can accept an explicit session or auto-create a default
    "Uploaded Videos" exam/session for direct uploads.

    Analyze video currently runs deterministic demo analysis through
    `run_demo_analysis`. It creates/updates AnalysisJob, Alert, Report, session
    status, and audit logs. This is a placeholder until the production AI
    worker is connected.

    Video history returns completed/analyzed videos.

    Dashboard endpoints return scoped counts and seven-day activity series.

    Threshold endpoints combine global SystemSettings values with optional
    per-user overrides stored in UserPreferences.metadata.

    Global threshold updates are admin-only and write audit logs.

    System logs and metrics are admin-only.

## Backend Structure

New helper modules:

    apis/selectors.py

        Centralizes role checks and scoped querysets:
        is_admin, users_visible_to, owned_sessions, owned_videos,
        owned_reports, recent_day_window.

    apis/services.py

        Holds workflow logic for audit logging, thresholds, upload session
        creation, demo analysis, dashboard stats/activity, and system metrics.

This keeps the view classes mostly focused on request/response handling.

## Frontend Integration

The frontend API layer now calls real backend endpoints instead of local
placeholder data for:

    Authentication
    Videos
    Dashboard stats and activity
    User activity
    Thresholds
    System logs and metrics
    Onboarding preferences

Notable frontend changes:

    src/api/auth.ts now uses /api/dj-rest-auth/... routes.

    src/api/videos.ts uploads files with FormData, maps backend UUID ids, and
    calls the analyze/history endpoints.

    src/api/dashboard.ts, src/api/system.ts, src/api/thresholds.ts, and
    src/api/users.ts now call backend APIs.

    src/api/onboarding.ts still stores onboarding locally, but also attempts to
    sync notification preferences and onboarding metadata to /api/me/preferences/.

    src/pages/Dashboard.tsx text now reflects that backend APIs are connected,
    while analysis output remains demo data.

    src/pages/VideoProcessing.tsx now treats video ids as strings to match
    backend UUIDs.

## Settings And Routing Notes

Backend settings now allow the Vite dev server origins:

    http://localhost:5173
    http://127.0.0.1:5173

The duplicate `django.template.context_processors.request` entry was removed.

In DEBUG mode, the backend serves uploaded media through:

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

## Migrations To Apply

Run from `no-more-cheaters-backend/nomorecheaters`:

    python manage.py migrate

Current relevant migrations:

    0002_analysisjob_report_alert_snapshot_url_and_more.py
    0003_userpreferences.py
    0004_alter_user_role.py

## Verification

Backend:

    python manage.py test apis

Result on 2026-05-22:

    44 tests passing

Frontend:

    npm run build

Result on 2026-05-22:

    TypeScript build and Vite production build completed successfully.

## Remaining Work

Production AI integration is still pending. The current analysis endpoint is a
deterministic demo workflow that preserves the expected database and API shape.

Expired video cleanup is still pending. `Video.expires_at` exists, but no
scheduled cleanup command/task has been added yet.
