# Gate — Event Ticketing & Door Vetting

FastAPI + Jinja2 + HTMX + SQLite (WAL). WhatsApp delivery via a self-hosted Evolution API container.

## Run locally (Docker)

```bash
cp .env.example .env
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/; s/^EVOLUTION_API_KEY=.*/EVOLUTION_API_KEY=$(openssl rand -hex 24)/; s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env
docker compose up -d --build
```

App: <http://localhost:8000> — default login `admin@event.local` / `admin1234` (change it: there is no
in-app user management yet, so add users/reset passwords via a Python shell using `app.auth.hash_password`).

## Configure the WhatsApp instance

```bash
set -a; . ./.env; set +a
# 1. create the instance (QR enabled)
curl -s -X POST http://localhost:8080/instance/create \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d "{\"instanceName\":\"$EVOLUTION_INSTANCE\",\"integration\":\"WHATSAPP-BAILEYS\",\"qrcode\":true}"
# 2. fetch a pairing code/QR (base64 PNG in .base64) and scan it in WhatsApp > Linked devices
curl -s http://localhost:8080/instance/connect/$EVOLUTION_INSTANCE -H "apikey: $EVOLUTION_API_KEY"
# 3. verify: state should be "open"
curl -s http://localhost:8080/instance/connectionState/$EVOLUTION_INSTANCE -H "apikey: $EVOLUTION_API_KEY"
```

Evolution's manager UI is also at <http://localhost:8080/manager> (host-only binding).

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pytest -q
```

## Deploy to Oracle Cloud Always-Free (ARM / Ampere A1)

Install Docker, copy the repo, set `PUBLIC_BASE_URL=https://your.domain` and `COOKIE_SECURE=true`,
and put a TLS reverse proxy (e.g. Caddy) in front of port 8000. Open only 80/443 in the VCN security list.
Back up the `app_data`, `app_storage` and `evolution_instances` volumes.

## Notes

- Doormen can view the event list and use the cockpit; only admins create events, import guests and upload PDFs.
- Claim links (`/tickets/claim/{token}`) are public by design (32-hex UUID4 tokens).
- The PDF page count must equal the guest list's total `ticket_count`; imports are all-or-nothing.
- The app runs one uvicorn worker (SQLite).
