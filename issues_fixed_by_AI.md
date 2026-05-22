# Issues Fixed by AI

This file tracks implementation issues fixed with AI assistance across the
backend and frontend projects in the `Senior Project` folder.

## Update - 2026-05-22

**Reviewer:** AI Agent (Codex)  
**Scope:** Full backend/frontend integration review  
**Backend tests:** 38 -> 44, all passing  
**Frontend verification:** `npm run build` passing  
**Files reviewed:** backend APIs, serializers, selectors, services, routes,
settings, tests, migrations, frontend API clients, Dashboard, VideoProcessing

---

## Backend / Frontend Integration Fixes

### Issue 15 - Frontend still depended on placeholder/local video APIs

**Problem:** The frontend video workflow had local-storage placeholder behavior,
but the backend now needs to own upload, analyze, detail, list, and history data.

**Fix:** Added backend video workflow endpoints and updated the frontend API
client to use them.

**Backend endpoints:**
- `GET /api/videos/`
- `POST /api/videos/upload/`
- `GET /api/videos/<uuid:pk>/`
- `POST /api/videos/<uuid:pk>/analyze/`
- `GET /api/history/`

**Frontend changes:**
- `src/api/videos.ts` now uses `apiFetch`, `FormData`, backend UUID ids, and
  backend report mapping.
- `src/pages/VideoProcessing.tsx` now tracks analyzing ids as strings.

**Files:** `views.py`, `urls.py`, `serializers.py`, `services.py`,
`selectors.py`, `tests.py`, `src/api/videos.ts`, `src/pages/VideoProcessing.tsx`

---

### Issue 16 - Missing backend workflow behind video analysis

**Problem:** The API needed an analysis route with the same database shape the
future AI worker will use, but production AI processing is not connected yet.

**Fix:** Added `run_demo_analysis` in `services.py`. It creates deterministic
demo output while preserving the intended workflow:
- session status moves through processing to completed
- `AnalysisJob` is created/updated
- an `Alert` is created
- a `Report` is created/updated
- audit logs are written for analysis start, completion, and report generation

**Note:** This is intentionally isolated behind a service boundary so the
production AI worker can replace it later without changing frontend API calls.

**Files:** `services.py`, `views.py`, `tests.py`

---

### Issue 17 - Dashboard and activity data were not backed by real API data

**Problem:** The frontend dashboard previously used placeholder counts and
generated local chart data.

**Fix:** Added scoped backend dashboard endpoints and connected the frontend.

**Backend endpoints:**
- `GET /api/dashboard/stats/`
- `GET /api/dashboard/activity/`

**Data returned:**
- users total
- exams total
- alerts total
- videos total
- videos processing
- videos completed
- analyses total
- cheating reports total
- seven-day videos/analyses series

**Files:** `services.py`, `views.py`, `urls.py`, `tests.py`,
`src/api/dashboard.ts`, `src/pages/Dashboard.tsx`

---

### Issue 18 - Threshold settings were local-only on the frontend

**Problem:** AI threshold settings were stored only in frontend local storage,
so they were not shared with the backend and could not support admin/global
settings.

**Fix:** Added backend threshold endpoints and wired the frontend to them.

**Backend endpoints:**
- `GET /api/thresholds/`
- `PATCH /api/thresholds/me/`
- `PATCH /api/thresholds/global/`

**Behavior:**
- Global thresholds are stored in `SystemSettings`.
- User-specific overrides are stored in `UserPreferences.metadata`.
- Effective thresholds merge global values with user overrides.
- Global updates are admin-only and create audit logs.
- Threshold values are validated as numeric values between 0 and 1.

**Files:** `services.py`, `views.py`, `urls.py`, `tests.py`,
`src/api/thresholds.ts`

---

### Issue 19 - System logs and metrics had frontend placeholders only

**Problem:** The frontend had system log and metric functions, but they returned
empty or placeholder data instead of backend values.

**Fix:** Added admin-only backend system endpoints and connected the frontend.

**Backend endpoints:**
- `GET /api/system/logs/`
- `GET /api/system/metrics/`

**Behavior:**
- System logs return paged audit-log rows.
- System metrics return timestamp, Python version, platform, and object counts.
- Both endpoints are admin-only.

**Files:** `views.py`, `services.py`, `urls.py`, `tests.py`, `src/api/system.ts`

---

### Issue 20 - User list/detail permissions were too broad for regular users

**Problem:** User endpoints used a broad queryset. Regular instructors should
not be able to enumerate or mutate other user accounts.

**Fix:** Added selector-based scoping:
- admins can see/manage all users
- instructors can only see their own user record
- non-admin update/delete attempts are denied
- delete writes an audit log

**Files:** `selectors.py`, `views.py`

---

### Issue 21 - User activity endpoint was missing

**Problem:** The frontend user activity chart had generated placeholder data.

**Fix:** Added `GET /api/<uuid:pk>/activity/`, returning seven-day upload and
analysis counts for the requested user. Admins can view anyone; instructors can
only view themselves.

**Files:** `selectors.py`, `views.py`, `urls.py`, `src/api/users.ts`

---

### Issue 22 - Auth frontend paths did not match mounted dj-rest-auth URLs

**Problem:** The frontend called short auth paths such as `/login/`, `/user/`,
and `/password/reset/`, but the backend exposes these under
`/api/dj-rest-auth/`.

**Fix:** Updated frontend auth calls to use the mounted backend paths:
- `/api/dj-rest-auth/login/`
- `/api/dj-rest-auth/registration/`
- `/api/dj-rest-auth/user/`
- `/api/dj-rest-auth/password/change/`
- `/api/dj-rest-auth/password/reset/`
- `/api/dj-rest-auth/password/reset/confirm/`

**Files:** `src/api/auth.ts`

---

### Issue 23 - Vite dev server was not allowed by backend CORS settings

**Problem:** The frontend dev server commonly runs on Vite port `5173`, but the
backend CORS whitelist only allowed ports `3000` and `8000`.

**Fix:** Added:
- `http://localhost:5173`
- `http://127.0.0.1:5173`

**Files:** `settings.py`

---

### Issue 24 - Uploaded media URLs needed development serving

**Problem:** Video serializers return file URLs, but Django was not configured
to serve uploaded media in DEBUG mode from the project URL config.

**Fix:** Added DEBUG-only static media serving:

```python
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
```

**Files:** `nomorecheaters/urls.py`

---

### Issue 25 - Custom user role had no default

**Problem:** The custom `User.role` field was required but had no model default,
which made account creation more fragile for flows that do not submit a role.

**Fix:** Added `default=User.Role.INSTRUCTOR` and generated migration
`0004_alter_user_role.py`.

**Files:** `models.py`, migration `0004_alter_user_role.py`

---

## Tests Added In This Update

New integration tests cover:
- direct video upload with automatic exam/session creation
- audit logging for video upload
- demo analysis creating job, alert, report, and completed session status
- video history after analysis
- dashboard stats for owned workflow counts
- personal threshold read/update
- admin-only global threshold updates
- admin system metrics and logs

**Verification:**

```bash
python manage.py test apis
```

Result:

```text
Found 44 test(s).
Ran 44 tests.
OK
```

Frontend verification:

```bash
npm run build
```

Result: TypeScript and Vite production build completed successfully.

---

## Update - 2026-05-11

**Reviewer:** AI Agent (Codex)  
**Tests:** 31 -> 38, all passing  
**Files changed:** `models.py`, `serializers.py`, `views.py`, `urls.py`,
`admin.py`, `tests.py`, migration `0003`

### Issue 11 - Missing per-user preferences support

**Problem:** The backend had a custom `User` model for account identity, but no
separate place to store per-user application preferences such as notifications,
language, timezone, theme, or frontend-specific metadata.

**Fix:** Added a dedicated `UserPreferences` model linked one-to-one with the
custom user model.

**Fields added:**
- `user` (OneToOne -> custom user model)
- `email_notifications`
- `dashboard_alerts`
- `preferred_language`
- `timezone`
- `theme`
- `metadata`
- `updated_at`

### Issue 12 - Preferences serializers needed ownership-safe fields

**Problem:** A preferences API should not allow clients to submit or change the
owning user id.

**Fix:** Added read/update serializers that expose only safe preference fields.

### Issue 13 - Missing authenticated `/me/preferences/` endpoint

**Problem:** There was no endpoint for an authenticated user to retrieve or
update their own preferences.

**Fix:** Added `GET` and `PATCH` support at `/api/me/preferences/`, with lazy
preference creation.

### Issue 14 - Preferences missing from Django admin

**Problem:** Admin users had no way to inspect or manage preference rows.

**Fix:** Registered `UserPreferences` in Django admin.

**Verification:** `python manage.py test apis`, 38 tests passing.

---

## Update - 2026-05-05

**Reviewer:** AI Agent (Antigravity)  
**Tests:** 21 -> 31, all passing  
**Files changed:** `models.py`, `serializers.py`, `admin.py`, `tests.py`,
migration `0002`

### Issue 2 - Missing `Report` model

Added `Report` for detailed analysis results, including session, probability,
alert counts, processing time, summary, and generated timestamp.

### Issue 3 - Missing `AnalysisJob` model

Added `AnalysisJob` to track queued, processing, completed, and failed analysis
jobs.

### Issue 4 - Missing video retention tracking

Added `Video.expires_at` for future retention cleanup.

### Issue 5 - Missing visual evidence field on alerts

Added `Alert.snapshot_url` for frame/snapshot evidence links.

### Issue 6 - Nullable FK serializer fields crashed on `None`

Added safe defaults for serializer fields that read nullable FK emails.

### Issue 7 - Duplicate video upload returned 500 instead of 400

Added duplicate hash validation in `VideoUploadSerializer`.

### Issue 8 - Missing exam update serializer

Added `ExamUpdateSerializer` for name/description updates.

### Issue 9 - AuditLog admin allowed deletion

Blocked audit-log deletion in admin.

### Issue 10 - Admin registration test was incomplete

Updated admin registration coverage for core models.

## Remaining Notes

Production AI processing is still pending. The current `/api/videos/<id>/analyze/`
route returns deterministic demo output while preserving the final database/API
shape.

Expired video cleanup is still pending. `Video.expires_at` exists, but no
scheduled cleanup command or background worker has been added yet.
