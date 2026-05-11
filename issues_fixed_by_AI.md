# Issues Fixed by AI - Code Review of Commit `ccbacc2`

## Update - 2026-05-11

**Reviewer:** AI Agent (Codex)  
**Tests:** 31 -> 38 (all passing)  
**Files changed:** `models.py`, `serializers.py`, `views.py`, `urls.py`, `admin.py`, `tests.py`, migration `0003`

---

## New Feature - Authenticated User Preferences

### Issue 11 - Missing per-user preferences support

**Problem:** The backend had a custom `User` model for account identity, but no separate place to store per-user application preferences such as notifications, language, timezone, theme, or frontend-specific metadata.

**Fix:** Added a dedicated `UserPreferences` model linked one-to-one with the custom user model.

**Fields added:**
- `user` (OneToOne -> custom user model)
- `email_notifications` (default `True`)
- `dashboard_alerts` (default `True`)
- `preferred_language` (default `en`)
- `timezone` (default `UTC`)
- `theme` (`LIGHT` / `DARK` / `SYSTEM`, default `SYSTEM`)
- `metadata` (JSON, default `{}`)
- `updated_at` (auto-updated timestamp)

**Files:** `models.py`, migration `0003_userpreferences.py`

---

### Issue 12 - Preferences serializers needed ownership-safe fields

**Problem:** A preferences API should not allow clients to submit or change the owning user id. Exposing a writable `user` field would create a cross-account update risk.

**Fix:** Added two serializers:
- `UserPreferencesReadSerializer` returns preference fields plus `user_email`, with all fields read-only.
- `UserPreferencesUpdateSerializer` accepts only preference fields and intentionally excludes `user`.

**Compatibility:** Added `UserPreferencesSerializer = UserPreferencesReadSerializer` alias for the current simple serializer naming pattern.

**Files:** `serializers.py`

---

### Issue 13 - Missing authenticated `/me/preferences/` endpoint

**Problem:** There was no endpoint for an authenticated user to retrieve or update their own preferences.

**Fix:** Added `MyPreferencesView` with:
- `GET /me/preferences/` to lazily create default preferences for the authenticated user and return them.
- `PATCH /me/preferences/` to update only the authenticated user's own preference row.

Authentication is enforced by the project's global DRF `IsAuthenticated` default permission setting.

**Files:** `views.py`, `urls.py`

---

### Issue 14 - Preferences missing from Django admin

**Problem:** Admin users had no way to inspect or manage `UserPreferences` rows.

**Fix:** Registered `UserPreferencesAdmin` with useful list columns, filters, search fields, user autocomplete, `updated_at` read-only, and `list_select_related` for user lookup efficiency.

**Files:** `admin.py`

---

## Tests Added / Updated

| # | Test Class | Test Name | What it covers |
|---|---|---|---|
| 1 | `UserPreferencesModelTests` | `test_user_preferences_defaults_are_sensible` | Default values for new preference rows |
| 2 | `UserPreferencesSerializerTests` | `test_read_serializer_does_not_expose_writable_user_id` | Read serializer hides writable ownership |
| 3 | `UserPreferencesSerializerTests` | `test_update_serializer_changes_preferences_without_changing_owner` | Update serializer changes preferences but preserves owner |
| 4 | `MyPreferencesAPITests` | `test_preferences_endpoint_requires_authentication` | Endpoint is protected |
| 5 | `MyPreferencesAPITests` | `test_get_preferences_creates_defaults_for_authenticated_user` | Lazy preference creation and read response |
| 6 | `MyPreferencesAPITests` | `test_patch_preferences_updates_authenticated_users_preferences` | Authenticated preference updates |
| 7 | `MyPreferencesAPITests` | `test_patch_preferences_cannot_change_owner` | PATCH input cannot move preferences to another user |

Updated existing `AdminRegistrationTests.test_core_models_are_registered_in_admin` to include `UserPreferences`.

**Verification:**
```bash
python manage.py test apis
```

Result: 38 tests passing.

### Migration Required

Run:
```bash
python manage.py migrate
```

This applies `0003_userpreferences.py`.

---

**Date:** 2026-05-05  
**Reviewer:** AI Agent (Antigravity)  
**Tests:** 21 → 31 (all passing ✅)  
**Files changed:** `models.py`, `serializers.py`, `admin.py`, `tests.py`, migration `0002`

---

## Bug Fixes (High Priority)

### Issue 6 — Nullable FK serializer fields crashed on `None`

**Problem:** `reviewed_by_email` in `AlertReadSerializer`, `user_email` in `AuditLogReadSerializer`, and `updated_by_email` in `SystemSettingsReadSerializer` all used `source='fk_field.email'` but the FK fields are nullable. When the FK is `None`, accessing `.email` on it raises an error during serialization.

**Fix:** Added `default=None` to each of these fields:
```python
# Before
reviewed_by_email = serializers.EmailField(source='reviewed_by.email', read_only=True)

# After
reviewed_by_email = serializers.EmailField(source='reviewed_by.email', read_only=True, default=None)
```

**Files:** `serializers.py` (lines with `reviewed_by_email`, `user_email`, `updated_by_email`)

---

### Issue 7 — Duplicate video upload returned 500 instead of 400 (FR4)

**Problem:** The `VideoUploadSerializer.create()` computed a SHA-256 hash and saved it to the DB with `unique=True`, but never checked for existing duplicates. A duplicate upload would hit the DB constraint and raise an unhandled `IntegrityError` (HTTP 500).

**Fix:** Added an explicit existence check before creating the Video:
```python
file_hash = digest.hexdigest()
if Video.objects.filter(file_hash=file_hash).exists():
    raise serializers.ValidationError(
        {'file': 'This video has already been uploaded (duplicate content).'}
    )
```

**Files:** `serializers.py` (`VideoUploadSerializer.create`)  
**Test:** `DuplicateVideoHashTests.test_duplicate_video_content_is_rejected`

---

### Issue 9 — AuditLog admin allowed deletion (FR14 violation)

**Problem:** `AuditLogAdmin` blocked `add` and `change` permissions but forgot `delete`. Audit logs should be fully immutable.

**Fix:** Added `has_delete_permission`:
```python
def has_delete_permission(self, request, obj=None):
    return False
```

**Files:** `admin.py` (`AuditLogAdmin`)

---

## New Models (Medium Priority)

### Issue 2 — Missing `Report` model (FR11)

**Problem:** Documentation requires "Generate detailed analysis reports" (FR11) and "Allow instructors to review alerts and reports" (FR12), but no Report model existed.

**Added:** `Report` model with fields:
- `session` (OneToOne → ExamSession)
- `overall_cheating_probability` (Float, validated 0.0–1.0)
- `total_alerts` (PositiveInteger)
- `alerts_by_type` (JSON — e.g. `{"PHONE_DETECTED": 2, "LOOKING_AWAY": 1}`)
- `processing_time_seconds` (Float)
- `summary` (Text)
- `generated_at` (auto timestamp)

**Files:** `models.py`, `serializers.py` (`ReportReadSerializer`, `ReportCreateSerializer`), `admin.py` (`ReportAdmin`)  
**Tests:** `ReportModelTests`, `ReportCreateSerializerTests`

---

### Issue 3 — Missing `AnalysisJob` model (FR5)

**Problem:** Documentation describes an offline video processing pipeline (upload → queue → AI process → store results) but there was no model to track processing jobs.

**Added:** `AnalysisJob` model with fields:
- `session` (OneToOne → ExamSession)
- `status` (QUEUED / PROCESSING / COMPLETED / FAILED)
- `ai_model_version` (CharField)
- `frame_sample_rate` (PositiveInteger, default=1)
- `started_at`, `completed_at` (nullable DateTimes)
- `error_message` (Text)
- `metadata` (JSON)

**Files:** `models.py`, `serializers.py` (`AnalysisJobReadSerializer`, `AnalysisJobCreateSerializer`), `admin.py` (`AnalysisJobAdmin`)

---

### Issue 4 — Missing video retention tracking (Privacy)

**Problem:** Documentation §5.1 states "videos are stored for a limited period, say 30 days, after which the videos are deleted." No field existed to track expiry.

**Added:** `Video.expires_at` (nullable DateTimeField) with a DB index for efficient cleanup queries.

**Files:** `models.py` (Video model), `serializers.py` (VideoReadSerializer fields)

---

### Issue 5 — Missing visual evidence field on Alert

**Problem:** Documentation mentions "a short snippet of the incident, providing a form of context" and "Review clear visual evidence" but Alert had no explicit field for frame snapshots.

**Added:** `Alert.snapshot_url` (URLField, max 500 chars, blank allowed). Included in both `AlertReadSerializer` and `AlertCreateSerializer` fields.

**Files:** `models.py` (Alert model), `serializers.py` (AlertReadSerializer, AlertCreateSerializer)

---

## Improvements (Low Priority)

### Issue 8 — Added `ExamUpdateSerializer`

**Problem:** Only `ExamCreateSerializer` and `ExamReadSerializer` existed. Instructors had no way to update exam name/description via the API.

**Added:** `ExamUpdateSerializer` with fields `['name', 'description']`.

**Files:** `serializers.py`

---

### Issue 10 — `AuditLog` missing from admin registration test

**Fix:** Added `AuditLog`, `AnalysisJob`, and `Report` to the `AdminRegistrationTests.test_core_models_are_registered_in_admin` test.

**Files:** `tests.py`

---

## New Tests Added (10 tests)

| # | Test Class | Test Name | What it covers |
|---|---|---|---|
| 1 | `DuplicateVideoHashTests` | `test_duplicate_video_content_is_rejected` | FR4 — duplicate video upload returns clean error |
| 2 | `ExamSessionDefaultStatusTests` | `test_default_status_is_pending` | Default session status |
| 3 | `ExamSessionDefaultStatusTests` | `test_status_can_be_set_to_processing` | Status transitions |
| 4 | `AlertCreateSerializerTests` | `test_instructor_cannot_create_alert_for_another_instructors_session` | Ownership authorization |
| 5 | `AlertCreateSerializerTests` | `test_owner_can_create_alert_for_own_session` | Positive ownership test |
| 6 | `UserRoleTests` | `test_role_must_be_valid_choice` | Role enum enforcement |
| 7 | `UserRoleTests` | `test_role_is_required` | Role not blank |
| 8 | `ReportModelTests` | `test_cheating_probability_must_be_between_zero_and_one` | Probability validator |
| 9 | `ReportModelTests` | `test_valid_report_can_be_created` | Report creation happy path |
| 10 | `ReportCreateSerializerTests` | `test_instructor_cannot_create_report_for_another_instructors_session` | Report ownership auth |

---

## Notes for Other Agents / Team Members

### Migration Required
A new migration `0002_analysisjob_report_alert_snapshot_url_and_more` was generated. Run:
```bash
python manage.py migrate
```

### Views & URLs Still Needed
The new models (`AnalysisJob`, `Report`) and new serializers (`ExamUpdateSerializer`, `AnalysisJobCreateSerializer`, `ReportCreateSerializer`) have **no views or URL routes yet**. The next step is to create:

1. **ExamViewSet** or dedicated views for CRUD on Exams (using `ExamCreateSerializer`, `ExamUpdateSerializer`, `ExamReadSerializer`)
2. **ExamSessionViewSet** with status transition logic
3. **VideoUploadView** endpoint
4. **AlertViewSet** with review action
5. **AnalysisJobViewSet** — create job, update status, list by exam
6. **ReportViewSet** — create report (called by AI pipeline), read by instructors
7. **AuditLogListView** — read-only, admin only
8. **SystemSettingsViewSet** — read for all, update for admin

### Video Expiry Cleanup
The `Video.expires_at` field is added but there is **no background task** to actually delete expired videos yet. Options:
- Django management command (`python manage.py cleanup_expired_videos`) run via cron
- Celery periodic task
- Cloud function triggered on schedule

### settings.py Note
There is a **duplicate** `django.template.context_processors.request` in `TEMPLATES.OPTIONS.context_processors` (lines 92 and 94 of settings.py). It's harmless but should be cleaned up.

### AI Pipeline Integration
The `AnalysisJob` model is designed to be updated by the AI processing module (Python/OpenCV/YOLO). The expected workflow is:
1. Instructor uploads video → `Video` created, `ExamSession.status = PENDING`
2. Backend creates `AnalysisJob` with `status = QUEUED`
3. AI worker picks up job → sets `status = PROCESSING`, records `started_at`
4. AI processes frames → creates `Alert` objects for each detection
5. AI completes → creates `Report`, sets `AnalysisJob.status = COMPLETED` and `ExamSession.status = COMPLETED`
6. On failure → sets `AnalysisJob.status = FAILED` with `error_message`
