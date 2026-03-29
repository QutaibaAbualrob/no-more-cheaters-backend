# Continue plan — migrations + environment check

**Date:** 2026-03-29  
**Goal:** Continue the Django backend plan: add database migrations, run `migrate`, verify `manage.py check`.

## Steps taken

1. **Dependencies:** Ran `pip install -r requirements.txt` (venv and global `py -3.10`).
2. **PyPI / pip:** Install failed with:
   - `ERROR: Could not find a version that satisfies the requirement Django<6,>=5.0 (from versions: none)` (venv)
   - Global Python had Django 5.2.1 already, but `djangorestframework` and even `wheel` reported `from versions: none` — **no packages reachable from PyPI** in this environment (network/firewall/mirror issue).
3. **Migrations:** Created initial migrations manually (PyPI unusable here, so `makemigrations` could not be run after installing DRF):
   - [`accounts/migrations/0001_initial.py`](../accounts/migrations/0001_initial.py) — custom `User` (depends on `auth` `0012_alter_user_first_name_max_length`)
   - [`proctoring/migrations/0001_initial.py`](../proctoring/migrations/0001_initial.py) — `SystemSettings`, `Video`, `AnalysisJob`, `AnalysisReport`, `Alert` (local `video_upload_to` in migration to avoid import/cycle issues)
   - [`audit/migrations/0001_initial.py`](../audit/migrations/0001_initial.py) — `AuditLog`
4. **`manage.py migrate`:** Failed immediately with:
   - `ModuleNotFoundError: No module named 'rest_framework'`
   - **Cause:** DRF is in `INSTALLED_APPS` but not installed on the interpreter used for `manage.py`.

## Errors / logs (verbatim)

```
ModuleNotFoundError: No module named 'rest_framework'
```

## Resolution / what you should run locally

When **pip can reach PyPI** (fix VPN/firewall/proxy if needed):

```powershell
cd e:\final_project\backend
py -3.10 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py check
```

If `makemigrations` reports a **model/migration drift** (e.g. `upload_to` duplicate function vs `models.py`), run:

```text
python manage.py makemigrations --dry-run
```

and only apply realignments if Django suggests real changes.

## Follow-ups

- [ ] Run `pytest` after deps install.
- [ ] Start Redis + `python manage.py rqworker default` for analysis jobs.
- [ ] Optional: expand tests (upload dedup, job flow) per plan.
