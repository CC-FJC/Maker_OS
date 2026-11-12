# MakerOS — Deployment with Docker + Tailscale (port 1313)

Two containers:

- **`data`** — holds all persistent state in named volumes: the SQLite
  database + backups (`/data`) and per-user private notes (`/private`).
- **`app`** — the platform itself (code baked into the image). It
  mounts the data volumes and bind-mounts `./content` (Markdown +
  uploads, so git keeps working on the host).

Updating the platform = rebuild the `app` image. The `data` volumes
are never touched, so users, events, notes, uploads and backups survive
every update.

## 1. Requirements

- Docker Desktop (Windows/Mac) or Docker Engine (Linux), with the
  Compose plugin (`docker compose`).
- Tailscale installed and logged in on the server machine.

## 2. Configure

```powershell
copy .env.example .env
notepad .env
```

Set at least:

```
MAKEROS_SECRET_KEY=<long random string>   # python -c "import secrets; print(secrets.token_urlsafe(32))"
MAKEROS_BASE_URL=https://<machine>.<tailnet>.ts.net
MAKEROS_COOKIE_SECURE=1
```

Your Tailscale URL: `tailscale status` → `DNSName` of this machine
(e.g. `https://fedora.tail120260.ts.net`).

## 3. Start

```powershell
docker compose up -d --build
```

The app listens on port **1313** on all interfaces:

- LAN: `http://<server-ip>:1313`
- Tailnet (run once on the server):

```powershell
tailscale serve --bg 1313
```

Then open `https://<machine>.<tailnet>.ts.net` from any tailnet device.
`tailscale serve` publishes HTTPS on 443 and forwards to
`127.0.0.1:1313`, so the public URL has no port in it.

## 4. Create the admin user

```powershell
docker compose exec app python -m makeros.cli adduser jacob --role admin
```

## 5. Update the platform (no data loss)

```powershell
git pull
docker compose up -d --build
```

Only the `app` container is rebuilt/recreated. `makeros-data` and
`makeros-private` volumes keep everything.

## 6. Useful commands

```powershell
docker compose ps                 # what is running
docker compose logs -f app        # app logs
docker compose exec app python -m makeros.cli reindex   # rebuild search index
docker compose stop               # stop everything (data stays)
docker compose down               # remove containers (data stays)
docker volume rm makeros-data makeros-private            # ONLY if you really want to wipe data
```

## 7. Google Calendar sync

Works because the Tailscale URL is real HTTPS. Configure once in `.env`
(it lives on the host — `docker compose up -d --build` never touches it):

```
MAKEROS_BASE_URL=https://<machine>.<tailnet>.ts.net
GOOGLE_REDIRECT_URI=https://<machine>.<tailnet>.ts.net/google/callback
GOOGLE_CALENDAR_ID=<calendar id, e.g. c_xxx@group.calendar.google.com>
```

Then:

1. In Google Cloud Console, register exactly that redirect URI in the
   OAuth client ("URIs de redirección autorizados").
2. Admin panel → Integraciones → paste Client ID / secret (stored in
   the DB volume, they also survive updates) → "Conectar con Google" →
   authorize.
3. The calendar ID from `GOOGLE_CALENDAR_ID` is used automatically;
   anything set in the admin panel wins over it.

Always open the platform via the Tailscale URL when doing the OAuth
login — entering via `http://<ip>:1313` makes Google reject the redirect.

## 8. Backups

The admin panel → Copias tab creates zip backups into `/data/backups`
(inside the `makeros-data` volume). To copy them off the server:

```powershell
docker compose cp app:/data/backups ./backups-out
```

Or set a recurring schedule in the same tab.

## Troubleshooting

- **Port 1313 already in use** on the host:
  `Get-NetTCPConnection -LocalPort 1313` (Windows) to see who owns it;
  stop the old process/container first. Inside Docker the port is
  always 1313, so only the host side can conflict.
- **Login loops back to the login page** — `MAKEROS_COOKIE_SECURE=1`
  while opening the app over plain `http://<ip>:1313`. Secure cookies
  need HTTPS; use the Tailscale URL, or set `MAKEROS_COOKIE_SECURE=0`
  in `.env` if you only ever use plain HTTP on the LAN.
- **"tailscale serve" missing** — the CLI must run on the server
  itself; it publishes that machine's localhost:1313.
- **After a Tailscale rename** the URL changes: update
  `MAKEROS_BASE_URL` in `.env`, the Google redirect URI, then
  `docker compose up -d`.
- **Data volumes** live in Docker's storage (`docker volume ls` shows
  `makeros-data`, `makeros-private`). Never `docker compose down -v`.
