# Deployment (production / EC2)

Reverse-proxy + process-manager config for running the backend on an EC2
instance. The app defaults to **MySQL** (see `settings.DATABASES`) and runs the
AI analysis pipeline **natively in-process** — with `RQ_ASYNC` off (the default)
an upload runs the analysis synchronously inside the request, so there is **no
Redis and no separate worker** to deploy. Two processes total: Gunicorn + Nginx.

> Why a reverse proxy at all (**C3**): with `DJANGO_DEBUG=false` (the production
> default) `urls.py` no longer serves `/media/` or `/static/` — Django's
> `static()` helper is DEBUG-only — so without Nginx serving those trees, every
> uploaded exam video, evidence snapshot/clip, and annotated analysis video
> returns **404**.

## Files

| File | Purpose | Install target |
|------|---------|----------------|
| `nginx.conf` | Serves `/static/` + `/media/` from disk, proxies the rest to Gunicorn. The `location /media/` block is the actual C3 fix. | `/etc/nginx/sites-available/nomorecheaters` |
| `gunicorn.service` | systemd unit running the Django WSGI app on `127.0.0.1:8000`. Analysis runs inside these workers — see the timeout note in the file. | `/etc/systemd/system/nomorecheaters.service` |

Both contain `ALL_CAPS` / `/PATH/TO/...` placeholders — fill them in before installing.

## EC2 host prerequisites

```sh
# Ubuntu / Debian
sudo apt update
sudo apt install -y python3-venv build-essential pkg-config \
    default-libmysqlclient-dev mysql-server nginx
# (Amazon Linux 2023: dnf install python3 gcc mariadb-connector-c-devel \
#  mariadb105-server nginx)
```

- **GPU** (optional, for the g4dn T4): install the CUDA torch build per
  `requirements.txt` after `pip install -r requirements.txt`.
- Open the EC2 **security group** to inbound 80/443 (and 22 for SSH).

## 1. Create the MySQL database

```sh
sudo mysql <<'SQL'
CREATE DATABASE nomorecheaters CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'nomorecheaters'@'localhost' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON nomorecheaters.* TO 'nomorecheaters'@'localhost';
FLUSH PRIVILEGES;
SQL
```

Using **RDS** instead? Skip the `mysql-server` install, point `DB_HOST` at the
RDS endpoint, and create the DB/user there.

## 2. App setup

```sh
cd /PATH/TO/no-more-cheaters-backend
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

cd nomorecheaters
cp .env.example .env        # then edit — see below
python manage.py migrate
python manage.py collectstatic --noinput   # populates STATIC_ROOT for Nginx
python manage.py createsuperuser            # optional
```

Required `.env` values for production (full list in `.env.example`):

```ini
DJANGO_SECRET_KEY=<generate a fresh one>
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=your-domain.com,<EC2 public IP>
DB_PASSWORD=<the MySQL password from step 1>
CORS_ALLOWED_ORIGINS=https://your-frontend.com
CSRF_TRUSTED_ORIGINS=https://your-frontend.com
FRONTEND_URL=https://your-frontend.com
```

> `DB_ENGINE`/`DB_NAME`/`DB_USER`/`DB_HOST`/`DB_PORT` default to local MySQL, so
> you usually only set `DB_PASSWORD` (and `DB_HOST` for RDS). Leave `RQ_ASYNC`
> unset/false so analysis runs natively in the Gunicorn worker — no Redis needed.

## 3. Install the service

```sh
sudo cp deploy/gunicorn.service /etc/systemd/system/nomorecheaters.service
# edit the ALL-CAPS / /PATH/TO placeholders first
sudo systemctl daemon-reload
sudo systemctl enable --now nomorecheaters
```

## 4. Install Nginx

```sh
sudo cp deploy/nginx.conf /etc/nginx/sites-available/nomorecheaters
sudo ln -s /etc/nginx/sites-available/nomorecheaters /etc/nginx/sites-enabled/
# edit server_name + the two /PATH/TO alias paths first
sudo nginx -t && sudo systemctl reload nginx
```

For HTTPS, run `certbot --nginx` (it rewrites this server block to listen 443
with the cert). The `SECURE_*` settings in `settings.py` activate automatically
once `DJANGO_DEBUG=false`.

## Verifying

```sh
# DB reachable + migrations applied
python manage.py migrate --check

# C3: a real evidence file under MEDIA_ROOT should return 200, NOT 404
curl -I https://YOUR_DOMAIN/media/snapshots/<session-id>/<alert-id>_crop.jpg

# App is up (analysis runs inside these workers)
sudo systemctl status nomorecheaters
```

A `/media/` 404 means the `location /media/` `alias` doesn't match `MEDIA_ROOT`
(`BASE_DIR/media`, where `BASE_DIR` is the dir containing `manage.py`).

## Known follow-up — media authorization

`location /media/` serves the media tree to anyone with the URL. Filenames are
UUID-based (not enumerable), but these are student exam recordings and face
crops. To gate downloads behind authentication, mark the `/media/` block
`internal;` and add a Django view that checks permissions and returns an
`X-Accel-Redirect` header pointing at the internal location. Out of scope for
C3 (which only restores serving); tracked as a follow-up.
