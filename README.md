# Gate — Event Ticketing & Door Vetting

FastAPI + Jinja2 + HTMX + SQLite (WAL). WhatsApp delivery through a self-hosted Evolution API container.
Runs on Docker Compose, locally or on an Oracle Cloud Always-Free ARM VM.

## What it does

- **Events** (admin): opponent picker (`מכבי תל אביב נגד …`), arena picker (default *בית (היכל מנורה מבטחים)*), calendar date/time.
- **Guests**: bulk import (`.xlsx` / `.json`) or add/edit/remove manually. Each guest has a **category**
  (חבר ארגון · מצטרף · פלוס · פתוח) and any mix of **מבוגר (adult)** and **נוער (youth)** tickets.
- **Tickets**: upload a merged PDF; it is split per page and handed out automatically to guests who lack tickets. Extra pages wait in a
  spare pool; new or edited guests get spare pages automatically, and if none are left you get a warning - upload more pages later and
  they are assigned. Removing a guest or lowering their count returns pages to the pool (with a fresh link).
- **Door cockpit** (admin + doorman): search, guests grouped by category with live counters, admitted total for all types
  at the top. WhatsApp and QR buttons both admit the guest; a small side button toggles admission only.
- **Users**: admins manage users; sign in with password and/or Google.

Import columns: `name`, `phone` (required); `category`, `adult`/`מבוגר`, `youth`/`נוער`, `email`, `age`, `notes`.
A legacy `tickets` column counts as adult tickets. Any invalid row rejects the whole file and lists every problem.
Phone numbers are stored in international format: local numbers get `DEFAULT_COUNTRY_CODE` (default `972`) and lose a leading `0`;
numbers written with `+` or `00` are kept.

## Run locally

```bash
cp .env.example .env
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/; s/^EVOLUTION_API_KEY=.*/EVOLUTION_API_KEY=$(openssl rand -hex 24)/; s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env
docker compose up -d --build
```

Open <http://localhost:8000>. Default login `admin@event.local` / `admin1234` — change it under **Account** right away.
To test QR codes / links from a phone on the same network, open the app from the server's LAN address
(e.g. `http://192.168.1.20:8000`) instead of `localhost`.

## Google sign-in

Only people already listed under **Users** can sign in (a Google account that isn't listed is refused), so nobody can
get in just by having a Google account.

1. <https://console.cloud.google.com> → create/select a project → **APIs & Services → OAuth consent screen**:
   User type *External*, fill app name + support email. Under **Audience**, either add your users as *Test users*
   or click **Publish app** (the `openid email profile` scopes need no Google review).
2. **Credentials → Create credentials → OAuth client ID → Web application**. Add **Authorized redirect URIs**:
   - `https://YOUR_DOMAIN/auth/google/callback` (production)
   - `http://localhost:8000/auth/google/callback` (local testing)
3. Put the client ID and secret in `.env`:
   ```
   GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   ADMIN_EMAIL=you@gmail.com       # optional: creates this admin at startup
   PUBLIC_BASE_URL=https://YOUR_DOMAIN   # must match the redirect URI host (empty locally)
   ```
4. `docker compose up -d` — the login page now shows **Sign in with Google**. Add the rest of your team under
   **Users** using their Google email (password optional). Once your own Google admin works, deactivate the default
   `admin@event.local` account.

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
# 2b. or link with a pairing code (Linked devices > Link with phone number instead)
curl -s "http://localhost:8080/instance/connect/$EVOLUTION_INSTANCE?number=972501234567" -H "$H"
# 3. verify: state should be "open"
curl -s http://localhost:8080/instance/connectionState/$EVOLUTION_INSTANCE -H "$H"
```

The manager UI is at <http://localhost:8080/manager> (bound to the host only; on a server use an SSH tunnel, see below).

**No QR / `{"count":0}`?** Use the current image (`evoapicloud/evolution-api:v2.3.7`, already in the compose file), delete and
recreate the instance (`curl -X DELETE http://localhost:8080/instance/delete/$EVOLUTION_INSTANCE -H "$H"`), and watch
`docker compose logs -f evolution-api`. As a last resort set `CONFIG_SESSION_PHONE_VERSION` to the current WhatsApp Web version.

WhatsApp sends are queued app-wide with at least `WHATSAPP_MIN_INTERVAL` seconds (default 1.5, hard floor 1.0, plus jitter)
between any two messages, to reduce the ban risk of the unofficial API.

### Ticket messages are deleted automatically

Every ticket the app sends is remembered (WhatsApp message id). It is then removed with WhatsApp's **delete for everyone**:
- **on admission** - when the guest is admitted (Admit button or QR), earlier ticket messages sent to them disappear, so a used ticket
  can't be forwarded. Tapping WhatsApp both admits and sends: the *new* message stays, anything sent earlier is deleted;
- **after 24 hours** - a background job deletes ticket messages older than `WHATSAPP_AUTO_DELETE_HOURS` (default 24; `0` disables).
  To keep WhatsApp traffic low it is **not** continuous: it wakes up every `WHATSAPP_CLEANUP_INTERVAL_HOURS` (default 12) and only contacts
  WhatsApp if an event took place within the last `WHATSAPP_CLEANUP_WINDOW_HOURS` (default 48), so deletion happens 24-36 h after sending.
  It gives up after 47 h, because WhatsApp stops allowing delete-for-everyone after about two days;
- when a guest is removed from the event.

**Phone numbers with more than one order are never auto-deleted** (admission, resend, removal and the timer all skip them), because
one chat can hold tickets for several orders.

`WHATSAPP_DELETE_ON_ADMIT=false` turns the on-admission deletion off. Evolution API does not expose WhatsApp's own *disappearing
messages* timer for one-to-one chats, which is why the app does the deleting itself. The guest will see "This message was deleted";
the app can only delete messages it sent after this feature was deployed.

## Deploy to Oracle Cloud Always-Free (ARM)

Result: `https://tickets.your-domain` served by Caddy (automatic HTTPS), app + Evolution + Postgres in Docker on an
Ampere A1 VM, all on the Always-Free tier.

### 1. Create the VM
1. Oracle Cloud console → **Compute → Instances → Create instance**.
2. Image: **Canonical Ubuntu 24.04** (aarch64). Shape: **VM.Standard.A1.Flex** (Ampere), e.g. **2 OCPU / 12 GB**
   (free up to 4 OCPU / 24 GB total). If it says *out of capacity*, retry later or try another availability domain.
3. Networking: a public subnet with a **public IPv4**. Upload/paste your SSH public key. Create.
4. Tip: reserve the public IP (**Networking → Reserved public IPs**) so it never changes.

### 2. Open ports 80 and 443
- **Cloud firewall:** VCN → Subnet → **Security List** → *Add ingress rules*: source `0.0.0.0/0`, TCP, destination ports `80` and `443`
  (port 22 is already open). Do **not** open 8000 or 8080.
- **OS firewall** (Oracle's Ubuntu images block everything else with iptables):
  ```bash
  sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
  sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
  sudo apt-get install -y iptables-persistent && sudo netfilter-persistent save
  ```

### 3. DNS
Point a hostname at the VM's public IP with an **A record** (your registrar, or a free name from <https://www.duckdns.org>).
Wait until `dig +short tickets.your-domain` returns the IP — Caddy needs it to get the certificate.

### 4. Install Docker and get the code
```bash
ssh ubuntu@YOUR_IP
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER && exit      # log in again afterwards
ssh ubuntu@YOUR_IP
git clone https://github.com/galmercado/gatepdfdispacher gate && cd gate
git checkout claude/event-ticketing-door-vetting-yw4336        # or main once merged
```

### 5. Configure
```bash
cp .env.example .env
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/; s/^EVOLUTION_API_KEY=.*/EVOLUTION_API_KEY=$(openssl rand -hex 24)/; s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env
nano .env     # set the values below
```
```
DOMAIN=tickets.your-domain
PUBLIC_BASE_URL=https://tickets.your-domain
COOKIE_SECURE=true
APP_BIND=127.0.0.1
GOOGLE_CLIENT_ID=...          # see "Google sign-in" (redirect URI: https://tickets.your-domain/auth/google/callback)
GOOGLE_CLIENT_SECRET=...
ADMIN_EMAIL=you@gmail.com
```

### 6. Start
```bash
docker compose --profile prod up -d --build
docker compose ps                      # app, evolution-api, postgres, caddy should be Up
docker compose logs -f caddy           # wait for "certificate obtained successfully"
```
Open `https://tickets.your-domain`, sign in with Google (or `admin@event.local` / `admin1234`, then **deactivate it**).

### 7. Link WhatsApp on the server
Evolution's port is bound to the server's localhost only. Reach it through an SSH tunnel from your laptop:
```bash
ssh -L 8081:localhost:8080 ubuntu@YOUR_IP      # leave open; then use http://localhost:8081/manager on your laptop
# (8081, not 8080: a local Evolution container on your laptop would otherwise answer instead, with a different API key -> "Unauthorized")
```
Run the *Configure the WhatsApp instance* commands on the server itself (`cd ~/gate` first, so `.env` is the right one) (or the manager UI through the tunnel) and scan the QR.
The session is stored in the `evolution_instances` + `postgres_data` volumes and survives restarts and updates.

### 8. Operate
```bash
git pull && docker compose --profile prod up -d --build      # update (data volumes are kept)
docker compose logs --tail=100 app                           # troubleshoot
./scripts/backup.sh ~/gate-backups                           # DB + ticket PDFs; keeps the newest 14
crontab -e   # nightly at 03:00:  0 3 * * * /home/ubuntu/gate/scripts/backup.sh >> /home/ubuntu/gate-backup.log 2>&1
```
Copy `~/gate-backups` off the server now and then (`scp`/`rsync`). To restore: stop the app, put `app-*.db` at
`/srv/data/app.db` and extract `storage-*.tar.gz` into `/srv` inside the `app` volumes.

### Pre-launch checklist
- [ ] `.env` has fresh random `SECRET_KEY`, `EVOLUTION_API_KEY`, `POSTGRES_PASSWORD` (never commit `.env`)
- [ ] `COOKIE_SECURE=true`, `PUBLIC_BASE_URL=https://…`, `APP_BIND=127.0.0.1`
- [ ] Default `admin@event.local` deactivated (or its password changed)
- [ ] Only ports 22, 80, 443 open; 8000 and 8080 are not reachable from the internet
- [ ] A QR code scanned from a phone opens the ticket PDF; a test WhatsApp message arrives
- [ ] Backup cron job installed and a backup restored at least once
- [ ] Oracle **reclaims idle Always-Free instances** (very low CPU/network for ~7 days). Upgrade the account to *Pay As You Go*
  (you stay at $0 within the free limits) if you want to be safe between events.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pytest -q
```

## Notes

- Doormen can view the event list and use the cockpit; admins create events, manage guests/tickets and users.
- QR codes link straight to the ticket PDF (`/tickets/claim/{token}/ticket.pdf`); the token is an unguessable 32-hex UUID4.
  `/tickets/claim/{token}` is a small mobile page with a Download button.
- Ticket PDFs are additive: each upload adds pages to the pool; tick *Start over* to replace everything.
- Team and arena lists live in `app/data.py`.
- One uvicorn worker (SQLite).
