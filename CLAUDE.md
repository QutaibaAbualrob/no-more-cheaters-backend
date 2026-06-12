# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This workspace holds two independent projects (each its own app; not a monorepo with shared tooling):

- `no-more-cheaters-backend/` — Django 5.2 + DRF API. The Django project root (where `manage.py` lives) is `no-more-cheaters-backend/nomorecheaters/`; the single app is `apis/`.
- `no-more-cheaters-frontend/` — React 19 + Vite + TypeScript SPA.

"No More Cheaters" is an exam-proctoring system: instructors upload exam videos, an analysis pass produces cheating Alerts and a Report, and admins manage users / thresholds / system logs.

## Commands

### Backend (run from `no-more-cheaters-backend/nomorecheaters/`)
```bash
python manage.py runserver          # dev server at http://127.0.0.1:8000
python manage.py migrate            # apply migrations
python manage.py makemigrations apis
python manage.py createsuperuser
python manage.py test apis          # full test suite (apis/tests.py)
python manage.py test apis.tests.<ClassName>.<test_method>   # single test
```
Dependencies: `pip install -r no-more-cheaters-backend/requirements.txt`. Note `requirements.txt` also lists the AI pipeline deps (`ultralytics`, `opencv-python-headless`) for future production analysis. The background queue (`django-rq`/`redis`) is now wired: analysis is enqueued via `services.enqueue_analysis` and runs in `apis/tasks.run_analysis`. It runs **synchronously in-process by default** (`RQ_ASYNC` off — no Redis/worker needed for dev or tests); set `RQ_ASYNC=true` with Redis and `python manage.py rqworker default` to process videos in the background.

### Frontend (run from `no-more-cheaters-frontend/`)
```bash
npm install
npm run dev       # Vite dev server at http://localhost:5173
npm run build     # tsc -b (typecheck) then vite build — use this to verify type safety
npm run lint      # eslint
npm run preview   # serve production build
```
There is no frontend test runner configured; `npm run build` is the type-safety gate.

## Backend architecture

The app deliberately separates concerns into layers — when adding behavior, put it in the right layer rather than fattening views:

- **`apis/models.py`** — domain hierarchy: `User → Exam → ExamSession → {Video (1:1), AnalysisJob (1:1), Alert (1:N), Report (1:1)}`, plus `AuditLog` (immutable), `SystemSettings` (key/value), `UserPreferences` (1:1 with User). Docstrings map models to functional requirements FR1–FR14.
- **`apis/selectors.py`** — read layer: role checks (`is_admin`) and ownership-scoped querysets (`owned_sessions`, `owned_videos`, `owned_reports`, `users_visible_to`). Non-admins only ever see their own data; admins see everything. Always scope queries through these helpers rather than querying models directly in views.
- **`apis/services.py`** — write/workflow layer: audit logging, threshold get/update, upload-session auto-creation, demo analysis, dashboard aggregates, system metrics. Mutating workflows live here (often `@transaction.atomic`), not in views.
- **`apis/serializers.py`** — per-model Read/Create/Update serializers; ownership validated in `validate_<fk>` methods; `VideoUploadSerializer` enforces allowed extensions and SHA-256 dedup.
- **`apis/views.py`** — thin DRF APIView/generics that delegate to selectors + services. Admin-only endpoints enforce `is_admin()` explicitly.
- **`apis/urls.py`** — mounted under `/api/`. Be aware user CRUD lives at the API root: `path('', UsersListView)` and `path('<uuid:pk>/', DeleteUserView)`.

Key conventions:
- **Custom user model** `AUTH_USER_MODEL = 'apis.User'`: UUID primary key, **email is the login field** (no separate username login), `role` is `ADMIN` or `INSTRUCTOR` (defaults to `INSTRUCTOR`). Always reference it via `get_user_model()`, never `django.contrib.auth.models.User`.
- **Roles drive everything**: backend access via `selectors.is_admin` / scoped querysets; mirror this when touching the frontend.
- **AI analysis is a placeholder, dispatched via the queue**: `AnalyzeVideoView` calls `services.enqueue_analysis`, which creates a `QUEUED` AnalysisJob and dispatches `apis/tasks.run_analysis` to the django-rq `default` queue. The worker calls `services.build_demo_report` (deterministic placeholder Alerts/Report) — replace just that to plug in a real AI worker without changing the queue, views, or API shape. Don't treat its output as real detection. Audit logging in the worker goes through `services.record_audit_log` (request-free).
- **Thresholds** combine global `SystemSettings` defaults (`THRESHOLD_DEFAULTS` in services) with optional per-user overrides stored in `UserPreferences.metadata` (JSON). Global updates are admin-only and audited.
- `AuditLog` is append-only (its admin disables add/change/delete); write entries via `services.write_audit_log`.
- Dev settings: `DEBUG=True`, SQLite, console email backend, token + session auth, CORS whitelisted for the Vite origins.

## Frontend architecture

- **Auth/transport**: `src/api/client.ts` `apiFetch` is the single fetch wrapper — injects the auth header (Token or Bearer), normalizes errors, and retries once on 401 after refreshing the token. Route API calls through it. Tokens/user persist in `localStorage` (`src/lib/authStorage.ts`, keys `nmc_tokens`/`nmc_user`).
- **Auth state**: `src/auth/AuthProvider.tsx` (`useAuth`) bootstraps from localStorage and exposes `signIn`/`signUp`/`signOut`/`refreshMe`. Route guards: `RequireAuth` (logged-in) and `RequireRole` (role-gated, redirects mismatches to `/app/dashboard`). Routing is defined in `src/App.tsx`.
- **API mapping layer** (`src/api/*.ts`): each module maps backend shapes → frontend types (e.g. backend `role` `ADMIN`→ frontend `"admin"`, else `"user"`). Auth hits `dj-rest-auth` routes (`/api/dj-rest-auth/...`).
- **Dev proxy**: `vite.config.ts` proxies `/api`, `/login`, `/logout`, `/user`, `/register`, `/password` to the backend at `http://127.0.0.1:8000`, so run both servers together.

> Caveat: some `src/api/*` modules may still use local/placeholder storage rather than live endpoints (`src/api/videos.ts` currently reads/writes `localStorage`), even though `notes.md` describes the backend integration. Verify whether a given module calls the real API before assuming it's wired up.

## Documentation in-repo

`no-more-cheaters-backend/notes.md` is the authoritative integration log (auth packages, endpoint list, migrations 0002–0004, threshold behavior, and remaining work: production AI integration + expired-video cleanup are still pending). Also see `issues_fixed_by_AI.md` and `docu.txt` in the backend.
