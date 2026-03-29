# No More Cheaters — Django API

## Prerequisites

- **Python 3.10–3.13** (recommended; avoid 3.14 until Django officially supports it)
- **Redis** (for django-rq background jobs). Optional: `docker compose up -d` using [docker-compose.yml](docker-compose.yml) to run Redis on port 6379.

## Setup

```powershell
cd e:\final_project\backend
py -3.10 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Initial **migrations** are committed under each app’s `migrations/` folder (`accounts`, `proctoring`, `audit`). After changing models, run `makemigrations` as usual.

Start **Redis**, then:

```powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

In a **second** terminal (same venv):

```powershell
python manage.py rqworker default
```

Without the worker, `POST /api/videos/{id}/analyze/` will enqueue jobs that stay queued until a worker runs.

## Configuration

Copy [`.env.example`](.env.example) to `.env`. Settings load variables from the environment and optional `.env` (no extra package required).

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Django secret |
| `DEBUG` | `True` / `False` |
| `DATABASE_URL` | Default: SQLite at `db.sqlite3` |
| `REDIS_URL` | Broker for django-rq |
| `MAX_UPLOAD_BYTES` | Upload cap (default 500MB) |

## API overview

| Method | Path | Notes |
|--------|------|--------|
| `POST` | `/api/auth/register/` | Instructor signup when `DEBUG` or `ALLOW_INSTRUCTOR_REGISTER` |
| `POST` | `/api/auth/token/` | JWT obtain (username/password) |
| `POST` | `/api/auth/token/refresh/` | JWT refresh |
| `GET` `POST` | `/api/videos/` | List / upload (multipart `file`) |
| `POST` | `/api/videos/{id}/analyze/` | Enqueue stub AI analysis |
| `GET` | `/api/reports/` | Analysis reports + nested alerts |
| `GET` `PATCH` | `/api/settings/` | System thresholds (admin only for `PATCH`) |
| `GET` | `/api/audit/` | Audit log (admin only) |
| `GET` | `/django-rq/` | RQ dashboard (use admin login in browser) |

## Maintenance

```powershell
python manage.py purge_old_videos
```

Deletes `Video` rows (and files) older than `retention_days` in `SystemSettings` (default 30).

## Tests

```powershell
pytest
```

## Troubleshooting

- **`pip` finds no packages** — check internet/VPN/firewall; try `pip install -i https://pypi.org/simple/ ...`.
- **Redis connection refused** — start Redis or set `REDIS_URL` to a reachable instance.
