# Gate — Event Ticketing & Door Vetting

FastAPI + Jinja2 + HTMX + SQLite (WAL). WhatsApp delivery via a self-hosted Evolution API container.

## Run locally (Docker)

```bash
cp .env.example .env
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/; s/^EVOLUTION_API_KEY=.*/EVOLUTION_API_KEY=$(openssl rand -hex 24)/; s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env
docker compose up -d --build
```

App: <http://localhost:8000> — default login `admin@event.local` / `admin1234` (change it under **Account**; admins manage users under **Users**).

## Configure the WhatsApp instance

```bash
set -a; . ./.env; set +a
H="apikey: $EVOLUTION_API_KEY"
# 1. create the instance
curl -s -X POST http://localhost:8080/instance/create -H "$H" -H "Content-Type: application/json" \
  -d "{\"instanceName\":\"$EVOLUTION_INSTANCE\",\"integration\":\"WHATSAPP-BAILEYS\",\"qrcode\":true}"
# 2a. QR: save it as a PNG and scan it (WhatsApp > Linked devices > Link a device)
curl -s http://localhost:8080/instance/connect/$EVOLUTION_INSTANCE -H "$H" \
  | python3 -c "import sys,json,base64;d=json.load(sys.stdin);open('qr.png','wb').write(base64.b64decode(d['base64'].split(',')[-1]))"
# 2b. or link with a pairing code instead (no camera needed): enter it under
#     Linked devices > Link with phone number instead
curl -s "http://localhost:8080/instance/connect/$EVOLUTION_INSTANCE?number=15551234567" -H "$H"
# 3. verify: state should be "open"
curl -s http://localhost:8080/instance/connectionState/$EVOLUTION_INSTANCE -H "$H"
```

The manager UI is at <http://localhost:8080/manager> (host-only binding).

### Troubleshooting: no QR / `{"count":0}`

1. Make sure you run the current image (`evoapicloud/evolution-api:v2.3.7`); the old `atendai/evolution-api:v2.2.3`
   ships an outdated WhatsApp Web version and never produces a QR. After pulling the update:
   `docker compose pull evolution-api && docker compose up -d`.
2. Delete the stuck instance and recreate it:
   `curl -X DELETE http://localhost:8080/instance/delete/$EVOLUTION_INSTANCE -H "$H"`, then repeat step 1.
3. Watch `docker compose logs -f evolution-api` while connecting; the QR arrives a few seconds after `connect`.
4. If it still fails, set `CONFIG_SESSION_PHONE_VERSION` (current WhatsApp Web version) under the
   `evolution-api` environment in `docker-compose.yml`, or try the pairing code (2b).

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
- Claim links (`/tickets/claim/{token}`) are public by design: a mobile landing page with a Download button (32-hex UUID4 tokens).
- The PDF may have more pages than needed (extras are stored, unassigned) but not fewer than the guest list's total `ticket_count`; imports are all-or-nothing.
- Phone numbers are stored in international format: local numbers get `DEFAULT_COUNTRY_CODE` (default `972`, Israel) prepended and a leading `0` dropped; numbers written with `+` or `00` are kept as-is. Existing guests are converted on startup.
- The app runs one uvicorn worker (SQLite).
- WhatsApp sends are queued app-wide with at least `WHATSAPP_MIN_INTERVAL` seconds (default 1.5, hard floor 1.0, plus random jitter) between any two messages, to reduce ban risk on the unofficial API.
