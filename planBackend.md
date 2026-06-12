# No More Cheaters — Backend Analysis

> Analysis of `no-more-cheaters-backend/` as of 2026-06-13.
> Last updated 2026-06-13: externalize-settings (step 1) and async-queue wiring (step 2) are complete.
> Scope: the Django 5.2 + DRF API only. The React frontend is out of scope except where it shapes the API contract.

---

## 1. What the backend is

An exam-proctoring API. Instructors upload exam-session videos; an analysis pass produces cheating **Alerts** and a summary **Report**; admins manage users, AI thresholds, and audit/system logs. Authentication is email-based with two roles (`ADMIN`, `INSTRUCTOR`).

The AI detection itself is **not implemented** — `services.build_demo_report` produces deterministic placeholder output behind a clean service boundary so a real worker can replace it without changing the API surface. Analysis is dispatched through a **django-rq** background queue (`apis/tasks.run_analysis`); see §4.

### Tech stack
| Concern | Choice |
|---|---|
| Framework | Django 5.2, Django REST Framework |
| Auth | `dj-rest-auth` + `django-allauth` (email login), DRF Token + Session auth |
| DB | SQLite (dev) |
| Storage | Local filesystem media (`MEDIA_ROOT`), served via `static()` in DEBUG |
| AI deps (declared, unused) | `ultralytics`, `opencv-python-headless` |
| Async queue (wired) | `django-rq` / `rq` / `redis` — eager in-process by default, Redis-backed worker when `RQ_ASYNC=true` |

### Project layout
- Django project root (where `manage.py` lives): `no-more-cheaters-backend/nomorecheaters/`
- Single app: `apis/`
- Settings/URLs/WSGI: `nomorecheaters/nomorecheaters/`

---

## 2. Architecture — layered by design

The app deliberately separates concerns. New behavior belongs in the right layer, not in fattened views.

```
HTTP  →  views.py        thin APIViews / generics; delegate only
         selectors.py    READ layer: role checks + ownership-scoped querysets
         services.py     WRITE/workflow layer: audit, thresholds, analysis, aggregates
         tasks.py        background worker: run_analysis owns the AnalysisJob lifecycle
         serializers.py  per-model Read / Create / Update + validation
         models.py       domain models, FR-mapped
```

### Domain model hierarchy
```
User (UUID pk, email login, role)
 ├── UserPreferences (1:1)        — UI/notification prefs + per-user threshold overrides (JSON metadata)
 └── Exam
      └── ExamSession  (unique student per exam)   status: PENDING→PROCESSING→COMPLETED/FAILED
           ├── Video        (1:1)  SHA-256 dedup, 30-day expires_at
           ├── AnalysisJob  (1:1)  QUEUED→PROCESSING→COMPLETED/FAILED
           ├── Alert        (1:N)  behavior_type, severity, confidence 0–1, review state
           └── Report       (1:1)  overall probability, alert breakdown, summary

AuditLog       — immutable, append-only (admin add/change/delete disabled)
SystemSettings — admin-configurable key/value (global AI thresholds)
```
Each model docstring maps to functional requirements FR1–FR14.

### Key conventions
- **Custom user model** `AUTH_USER_MODEL = 'apis.User'`: UUID pk, **email is the login field**, `role` defaults to `INSTRUCTOR`. Always referenced via `get_user_model()`.
- **Ownership scoping**: non-admins only ever see their own data; admins see everything. Always go through `selectors` (`owned_sessions`, `owned_videos`, `owned_reports`, `users_visible_to`) rather than querying models directly.
- **Thresholds**: global `SystemSettings` defaults (`THRESHOLD_DEFAULTS` in services) merged with optional per-user overrides in `UserPreferences.metadata['thresholds']`. Global writes are admin-only and audited.
- **AuditLog** is write-only via `services.write_audit_log` (captures IP + user-agent).

---

## 3. API surface

Mounted under `/api/` ([apis/urls.py](no-more-cheaters-backend/nomorecheaters/apis/urls.py)). Note **user CRUD lives at the API root**.

| Method | Path | View | Access |
|---|---|---|---|
| GET | `/api/` | `UsersListView` | auth; admin sees all, instructor sees self |
| GET/PUT/PATCH/DELETE | `/api/<uuid>/` | `DeleteUserView` | read auth; mutate/delete admin-only |
| GET | `/api/<uuid>/activity/` | `UserActivityView` | self or admin |
| GET/PATCH | `/api/me/preferences/` | `MyPreferencesView` | auth (own row, lazily created) |
| GET | `/api/videos/` | `VideoListView` | scoped |
| POST | `/api/videos/upload/` | `VideoUploadView` | scoped; multipart |
| GET | `/api/videos/<uuid>/` | `VideoDetailView` | scoped |
| POST | `/api/videos/<uuid>/analyze/` | `AnalyzeVideoView` | scoped; enqueues analysis (200 eager / 202 async) |
| GET | `/api/history/` | `VideoHistoryView` | scoped, COMPLETED only |
| GET | `/api/dashboard/stats/` | `DashboardStatsView` | scoped aggregates |
| GET | `/api/dashboard/activity/` | `DashboardActivityView` | scoped 7-day series |
| GET | `/api/thresholds/` | `ThresholdsView` | global+user+effective |
| PATCH | `/api/thresholds/me/` | `MyThresholdsView` | own overrides |
| PATCH | `/api/thresholds/global/` | `GlobalThresholdsView` | **admin-only** |
| GET | `/api/system/logs/` | `SystemLogsView` | **admin-only**, paged |
| GET | `/api/system/metrics/` | `SystemMetricsView` | **admin-only** |

Auth routes (via `dj-rest-auth`/`allauth`): `/api/dj-rest-auth/login|logout|user|password/...` and `/api/dj-rest-auth/registration/`.

**Default permission** is `IsAuthenticated` globally; admin-only endpoints additionally enforce `is_admin()` explicitly in the view.

---

## 4. Notable workflows

- **Direct upload** ([services.get_available_upload_session](no-more-cheaters-backend/nomorecheaters/apis/services.py)): when no session is supplied, auto-creates an "Uploaded Videos" exam and a uniquely-suffixed session that has no video yet.
- **Dedup (FR4)**: `VideoUploadSerializer.create` streams the file through SHA-256 and rejects duplicate content; sets `expires_at = now + 30 days`.
- **Queued analysis** ([services.enqueue_analysis](no-more-cheaters-backend/nomorecheaters/apis/services.py)): upserts a `QUEUED` AnalysisJob, flips the session to PROCESSING, emits an ANALYSIS_STARTED audit entry, then dispatches [tasks.run_analysis](no-more-cheaters-backend/nomorecheaters/apis/tasks.py) on the django-rq `default` queue. The worker owns the job lifecycle (PROCESSING → COMPLETED/FAILED), calls [services.build_demo_report](no-more-cheaters-backend/nomorecheaters/apis/services.py) for the deterministic `LOOKING_AWAY` alert + Report, and emits ANALYSIS_COMPLETED + REPORT_GENERATED audit entries via the request-free `record_audit_log`. On failure it marks the job/session FAILED (outside the rolled-back transaction) and re-raises.
  - **Sync vs async** ([AnalyzeVideoView](no-more-cheaters-backend/nomorecheaters/apis/views.py)): with `RQ_ASYNC` off (dev/test default) the worker runs in-process and the endpoint returns `200` with the finished report; with `RQ_ASYNC=true` + Redis + an `rqworker`, the job is handed off and the endpoint returns `202 Accepted` with `{job_id, status}` for the client to poll. The eager path bypasses rq's own sync mode (which still touches Redis), so no external services are needed for dev or tests.

---

## 5. Testing

`apis/tests.py` is comprehensive (~44 tests per `notes.md`, last green 2026-05-22): model constraints, serializer validation + ownership/admin-bypass, admin registration, and full HTTP integration tests (auth required, UUID routing, upload→analyze→history→dashboard, threshold admin-gating, system logs/metrics). Uses temp `MEDIA_ROOT` and in-memory SQLite. No frontend test runner — `npm run build` is the frontend type gate.

Run: `python manage.py test apis` from `no-more-cheaters-backend/nomorecheaters/`.

---

## 6. Findings & risks

### ✅ Resolved
1. ~~**`DEBUG = True`, hardcoded `SECRET_KEY`, empty `ALLOWED_HOSTS`**~~ — Settings are now fully env-driven via `env()`/`env_bool()`/`env_list()` helpers. `DEBUG` defaults to `False`; `SECRET_KEY` reads from `DJANGO_SECRET_KEY`; `ALLOWED_HOSTS` reads from `DJANGO_ALLOWED_HOSTS`.
2. ~~**No `AUTHENTICATION_BACKENDS` set**~~ — Both `ModelBackend` and `allauth.account.auth_backends.AuthenticationBackend` are configured.
3. ~~**CORS config likely ineffective**~~ — Settings now use `CORS_ALLOWED_ORIGINS` (the correct key); Vite dev origins are whitelisted.
4. ~~**Async queue commented out / analyze runs synchronously**~~ — `django-rq` is wired. `AnalyzeVideoView` calls `services.enqueue_analysis` → `tasks.run_analysis`. Eager in-process by default (`RQ_ASYNC` off); real Redis-backed worker with `RQ_ASYNC=true`. 47 tests green; no Redis required for dev or tests.

### 🔴 Production-blocking (config)
5. **SQLite** — fine for dev; not for concurrent production load.

### 🟠 Functional gaps (known, per `notes.md`)
6. **Real AI pipeline is not implemented** — `tasks.run_analysis` calls `services.build_demo_report` (deterministic placeholder). The `ultralytics`/OpenCV deps are declared but unused. Replace `build_demo_report` to plug in a real worker; the queue, views, and API shape need no changes.
7. **No expired-video cleanup** — `Video.expires_at` is set but nothing deletes expired files/rows. Needs a scheduled management command or queued task (the 30-day privacy policy is unenforced).

### 🟡 Smaller observations
8. **Registration role assignment** — `dj-rest-auth` registration is mounted, but there's no custom registration serializer shown that sets `role`; new signups default to `INSTRUCTOR`. Confirm admins can only be created via `createsuperuser`/admin, and that role can't be self-elevated.
9. **`SystemLogsView` pagination** is hand-rolled (manual slicing + `count()`); fine, but DRF pagination would be more consistent and avoid the double query.
10. **`system_metrics` `uptime_seconds` is hardcoded to 0** — placeholder.
11. **`UserActivityView` duplicates** the activity-series logic that already exists in `services.activity_series_for` rather than reusing it — minor drift risk.

---

## 7. Suggested next steps (priority order)

1. ✅ **Externalize settings** *(complete 2026-06-13)* — `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, DB, CORS, and queue config are all env-driven via `env()`/`env_bool()`/`env_list()` helpers. `CORS_ALLOWED_ORIGINS` and `AUTHENTICATION_BACKENDS` are set correctly.
2. ✅ **Wire the async queue** *(complete 2026-06-13)* — `django-rq`/`rq`/`redis` added to requirements; `django_rq` in `INSTALLED_APPS`; `RQ_QUEUES`/`RQ` configured; `services.enqueue_analysis` creates the `QUEUED` job, dispatches `tasks.run_analysis`; `AnalyzeVideoView` returns `200 + report` (eager) or `202 Accepted` (async); 3 new tests added, 47 total green.
3. **Implement the real AI worker** — replace `services.build_demo_report` with a real YOLO/OpenCV pass (`ultralytics` dep already declared); populate Alerts and Report with the same model shapes. The queue, views, serializers, and API contract need no changes.
4. **Add expired-video cleanup** — management command (`cleanup_expired_videos`) on a schedule, deleting files + rows past `Video.expires_at`, audited.
5. **Harden registration** — explicit role control; verify the live `dj-rest-auth` login/registration flow with the custom user model end-to-end.
6. **Production posture** — Postgres, real media storage (e.g. S3), DRF pagination across list endpoints, real `uptime_seconds` in metrics.

---

## 8. Quick reference — commands

From `no-more-cheaters-backend/nomorecheaters/`:
```bash
python manage.py runserver        # dev server :8000
python manage.py migrate
python manage.py makemigrations apis
python manage.py createsuperuser
python manage.py test apis        # full suite
```
Deps: `pip install -r no-more-cheaters-backend/requirements.txt`
```
