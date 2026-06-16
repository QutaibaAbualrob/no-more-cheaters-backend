# Deployment (production)

This directory holds the reverse-proxy and process-manager config the app needs
in production. It exists to fix **C3**: with `DJANGO_DEBUG=false` (the production
default), `urls.py` no longer serves `/media/` or `/static/` — Django's
`static()` helper is DEBUG-only — so without a reverse proxy serving those trees,
every uploaded exam video, evidence snapshot/clip, and annotated analysis video
returns **404**.

## Files

| File | Purpose | Install target |
|------|---------|----------------|
| `nginx.conf` | Serves `/static/` + `/media/` from disk, proxies the rest to Gunicorn. The `location /media/` block is the actual C3 fix. | `/etc/nginx/sites-available/nomorecheaters` |
| `gunicorn.service` | systemd unit running the Django WSGI app on `127.0.0.1:8000`. | `/etc/systemd/system/nomorecheaters.service` |

Both files contain `ALL_CAPS` / `/PATH/TO/...` placeholders — fill them in before installing.

## Order of operations

1. `DJANGO_DEBUG=false` and the prod env vars (secret key, DB/Redis URLs,
   `DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `FRONTEND_URL`) are set in the
   `.env` that `gunicorn.service` loads.
2. `python manage.py collectstatic --noinput` → populates `STATIC_ROOT`
   (`BASE_DIR/staticfiles`), which `nginx.conf`'s `/static/` block serves.
3. Install + start `gunicorn.service`.
4. Start the **rqworker** (separate process — see note in `gunicorn.service`)
   so queued AI analysis jobs actually run.
5. Install `nginx.conf`, `sudo nginx -t`, reload Nginx.

## Verifying the C3 fix

After deploy, with a real evidence file on disk under `MEDIA_ROOT`:

```sh
# Should return 200 with the image bytes, NOT 404.
curl -I https://YOUR_DOMAIN/media/snapshots/<session-id>/<alert-id>_crop.jpg
```

A 404 means the `location /media/` `alias` path doesn't match `MEDIA_ROOT`
(`BASE_DIR/media`, where `BASE_DIR` is the dir containing `manage.py`).

## Known follow-up — media authorization

`location /media/` serves the media tree to anyone with the URL. Filenames are
UUID-based (not enumerable), but these are student exam recordings and face
crops. To gate downloads behind authentication, mark the `/media/` block
`internal;` and add a Django view that checks permissions and returns an
`X-Accel-Redirect` header pointing at the internal location. Out of scope for
C3 (which only restores serving); tracked as a follow-up.
