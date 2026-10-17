# Gate — Event Ticketing & Door Vetting

FastAPI + Jinja2 + HTMX + SQLite (WAL). WhatsApp delivery through a self-hosted Evolution API container.
Runs on Docker Compose, locally or on an Oracle Cloud Always-Free ARM VM.

## What it does

- **Events** (admin; can be deleted with all their guests, tickets and PDFs): opponent picker (`מכבי תל אביב נגד …`), arena picker (default *בית (היכל מנורה מבטחים)*), calendar date/time.
- **Guests**: bulk import (`.xlsx` / `.json`) or add/edit/remove manually. Each guest has a **category**
  (חבר ארגון · מצטרף · פלוס · פתוח) and any mix of **מבוגר (adult)** and **נוער (youth)** tickets.
- **Tickets**: upload a merged PDF (a progress bar with percent and a Cancel button shows while it uploads); it is split per page and handed out automatically to guests who lack tickets. Extra pages wait in a
  spare pool; new or edited guests get spare pages automatically, and if none are left you get a warning - upload more pages later and
  they are assigned. Removing a guest or lowering their count returns pages to the pool (with a fresh link).
- **Partial entry**: *Partial send* on a card (orders with 2+ tickets) opens a popup to choose how many adult/youth tickets to hand out now, by
  WhatsApp or QR. Only those tickets are admitted ("Partial 1/3"); later the WhatsApp/QR buttons send the rest, or send another partial amount.
  Undo resets the whole order.
- **QR at the door**: one QR code per hand-out whose link downloads a single ZIP with all the ticket PDFs being handed over (a partial send zips only the chosen ones;
  un-admit kills the link).
- **Door cockpit** (admin + doorman): search, guests grouped by category with live counters, admitted total for all types
  at the top. WhatsApp and QR buttons both admit the guest; a small side button toggles admission only. Admins also get an *Edit* button on every card (e.g. to change the ticket amount); doormen can only admit.
- **Users**: admins manage users; sign in with password and/or Google.

Import columns (spreadsheet headers *and* JSON keys; case/space/underscore insensitive, e.g. `adult`, `Adults`, `adult_count`, `מבוגר` all work): `name`, `phone` (required); `category`, `adult`/`מבוגר`, `youth`/`נוער`, `email`, `age`, `notes`.
A legacy `tickets` column counts as adult tickets. Columns/keys that are not recognised are reported after the import instead of being silently ignored. `age` may be a decimal (20.5). Valid rows are imported immediately; **re-importing a list updates existing guests** (same name + phone) with the fields the file contains - e.g. changed ticket counts or category - and leaves fields the file doesn't mention alone; rows with problems
are listed on the Manage page so you can correct them and add the guest, or skip them (one by one or *Skip all*). A file that cannot be read at all
is rejected with a message.
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

Every ticket the app sends over WhatsApp is remembered (message id) and later removed with WhatsApp's **delete for everyone**:
- **When you un-admit an order** - that order's ticket messages are deleted right away and its QR links stop working (new tokens
  are issued, so a QR shown earlier returns "not found"). Only *that order* is touched: another order on the same phone number,
  admitted or not, keeps its messages and links.
- **After the event** - every message gets its own random deletion time **12-36 hours after the event is over**
  (event start + `EVENT_DURATION_HOURS`, default 3), so deletions are spread out instead of one burst. Deletions are done one at a
  time with random 3-15 s pauses on top of the 1.5 s minimum gap. The background job wakes up every 10 minutes but only checks the
  database; WhatsApp is contacted only when a message is actually due. Failed deletions are retried after 30-90 min and given up
  after 47 h.
- When an order is removed from the event, its messages are deleted too.

Admitting someone (Admit, WhatsApp or QR button) deletes nothing. `WHATSAPP_DELETE_ON_UNADMIT=false` / `WHATSAPP_DELETE_AFTER_EVENT=false`
switch the two behaviours off; `WHATSAPP_DELETE_MIN_HOURS` / `WHATSAPP_DELETE_MAX_HOURS` change the 12-36 h window.

Limits: WhatsApp only lets you delete for everyone for roughly two days after sending, so a ticket message sent many days *before*
the event can no longer be deleted afterwards. Evolution API does not expose WhatsApp's own disappearing-messages timer for
one-to-one chats, which is why the app does the deleting itself. Guests see "This message was deleted". Only messages sent after
this feature was deployed can be deleted.

## Door: shirt missing and partial send

* **Shirt missing** (every doorman and admin, on each guest with tickets still to come): choose how many tickets are missing a shirt, then
  send the guest the **payment link** by WhatsApp or show its **QR** to scan (the link and message are on the Settings page; the default is the Bit link).
  Those tickets are **held**: the card turns yellow, and the WhatsApp / QR / partial / admit buttons hand out only the other tickets. Held tickets
  cannot be sent by anyone until an **admin** presses *Shirt confirmed - release*. Example: John ordered 2, Jane has no shirt - mark 1 missing; John gets his
  ticket now, Jane's stays held.
* **Partial send** (orders of 2+ tickets): choose how many adult/youth tickets to send now. The main WhatsApp/QR buttons then send the rest, or send more
  partially later. Every QR is a single link to one ZIP of the tickets handed out in that step.

## Ticket files: Google Drive, automatic upload API, compression, retention

**Google Drive.** On an event's Manage page, under *Upload ticket PDF*, paste one or more Drive share links (one per line).
The file must be shared as *Anyone with the link* and be a PDF; a folder link is refused. The app downloads it and adds it
to the ticket pool exactly like an uploaded file.

**Automatic upload API** (for another service). Set `TICKETS_API_KEY` in `.env` (e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
and restart. Without a key the API is off (HTTP 503).

```bash
curl -X POST https://gatetickets.duckdns.org/api/tickets \
  -H "X-API-Key: $TICKETS_API_KEY" \
  -F "file=@tickets.pdf" \
  -F 'header=קיבלת כרטיסים למכבי ראפיד ת"א - עירוני קריית אתא - לא מסומן'
```

* `header` gives the opponent (the team that is not Maccabi Tel Aviv; spellings such as `ת"א`, `קרית`/`קריית` are matched
  against the opponents list). Instead of `file` you can send `url` (a Drive link). `Authorization: Bearer <key>` works too.
* The **date, time and venue** are read from the first page of the PDF (dd/mm/yyyy, dd.mm.yy, yyyy-mm-dd or "17 באוקטובר 2026";
  venue = any name from the venues list). The game is matched by opponent and day; if it does not exist it is **created**
  (venue defaults to the home arena when none is found). If the PDF has no readable date, the nearest upcoming game
  against that opponent is used, otherwise the call fails with 422.
* Pages are added to the pool and handed to guests like any upload. Sending the same file again changes nothing
  (`"duplicate": true`). The answer is JSON: `event_id`, `event`, `date`, `location`, `created_event`, `pages`, `assigned`, `spare`, `missing`.

**Compression.** Every ticket page is shrunk when the PDF is split (Settings → *Compress ticket PDFs*): page data is
deflated, duplicate objects removed, huge images scaled to at most `PDF_IMAGE_MAX_PX` (1600) pixels. Flat images such
as QR codes and barcodes are stored **losslessly**; only photo-like images are JPEG-compressed (`PDF_JPEG_QUALITY`, 80).
A re-encoded image is used only if it is really smaller. Tests decode the QR code from the compressed pages.

**Retention.** Ticket PDFs are deleted `TICKET_RETENTION_DAYS` (default 30) after the event ends, checked every 10 minutes
(Settings → Cleanup; 0 keeps them). Guests and attendance stay; the tickets lose their file, so old download links stop working.

### A line stuck on "connecting" / "conflict - replaced" loop

If the Evolution log shows `CONNECTED TO WHATSAPP` twice per second together with `stream:error conflict type="replaced"`, two sockets
use the same WhatsApp login and kick each other out. The QR popup no longer asks Evolution to connect while a session is closed or restarting
(WhatsApp restarts it right after the scan), which was one way to cause this. To clear a loop that is already running:
`docker compose restart evolution-api`, or on the Lines page press **Reset session** for that line and scan the QR code **once**.

## Site settings (admin)

The **Settings** page (sliders icon) edits the main constants without touching files: the **ticket message wording** (greetings, main sentence, ticket counter, date line,
closings, emoji - English and Hebrew sets, with a live preview), WhatsApp pacing (gap, random jitter, typing time, daily limit per line, minimum working lines, line-test lead
time), message deletion (on un-admit / after the event, the random delay window, event length), and the event form (home team, opponents, venues, default venue, time zone,
default phone country code). Changes apply immediately, are stored in the database and survive restarts; *Reset* restores a section's built-in defaults (the values from `.env`/code).

## Import from the registration system (gate11.co.il)

On an event's **Manage** page, section *Import from the registration system*. Three ways, all ending in the same import:

1. **Popup + paste (no setup):** enter the game ID, press *Open gate11.co.il* (opens the reservations page in a popup; sign in with Google there as usual), press
   Ctrl+A then Ctrl+C on that page, come back and press Ctrl+V on the Manage page. (A website can never read another site's login cookie - the clipboard is the
   simplest way to hand over what you see.)
2. **“Send to Gate” bookmark:** one click instead of copy/paste - drag the button to your bookmarks bar once; on the reservations page click it and press Import in the small window.
3. **Saved page / session cookie (advanced):** upload the page saved from the browser, or paste the `Cookie` header once and use *Fetch & import*.

What the import understands from the registration page: the section titles (חברי ארגון / מצטרפים / פלוסים / פתוח) become each guest's type; the repeated header
rows, totals and spacer rows are ignored; מבוגר / נוער become the adult / youth tickets; הסעה > 0 is added to the notes; the site's default note "אתר ארגון" is dropped;
the reservation id (מזהה, the order number) is kept - hidden on the guest cards and shown only while you search (you can also search by it), so **two orders with the same name and phone stay two guests** and a later import updates the same order even if its name or
phone changed. An age that is not a number is left empty. New reservations are added, changed ones updated, rows with problems wait in the fix-or-skip panel.

### Members phone book (חברי ארגון)

Admins get a **Members** page (address-book icon): name + phone for each member (add one by one, paste lines, or upload .xlsx/.csv/.json). Whenever a reservation
arrives **without a valid phone number** - registration import, files, manual entry - the number is filled in by matching the name (first/last name order and quotes don't
matter; a name listed twice is never matched; פלוס guests never get a member's number). Optionally the list's number is always used for members. After adding numbers, press **Retry waiting rows** on the
event's fix-or-skip panel to import everything that was waiting for a phone number.

## WhatsApp lines (several numbers, fail-over)

Admins get a **Lines** page (SIM-card icon in the header). Each line is one WhatsApp number = one Evolution instance:
- **Add one line / Top up to 10 lines** creates the Evolution instances; press **Connect / QR** on a line and scan its QR code
  (or use a pairing code) with the phone that holds that number. Your original `EVOLUTION_INSTANCE` is registered as line "Main".
- Tickets go out through the first working line (**Make first** reorders). Every line has a daily limit (`LINE_DAILY_CAP`, default 150);
  when it is reached the next line is used.
- **Fail-over:** if a line errors with HTTP 5xx / a dropped session, the same message is retried on the next line. Only if that
  next line *delivers* is the first one marked **Not functioning** - so one bad guest number can never mark every line as dead. A 4xx
  (bad recipient) or an outage on all lines marks nothing.
- **24 h before every event** each line is tested - as a short, ordinary chat **between your own lines**: every connected line messages the next one in a ring and the
  receiver answers with a short reply, so each number shows normal activity (the lines' phone numbers are read from Evolution automatically). Only when no other
  connected line is available does a line write to the optional backup **test number** (Lines page). A line that recovers is switched back to Working by the next test.
  **Test all lines now** runs it on demand.
- **Debugging (HTTP 500 etc.):** every line has a **Diagnose** button. It checks, step by step and showing Evolution's raw answer for each: connection state, the number
  linked, whether the target is registered on WhatsApp, a text *without* the "typing..." simulation and a text *with* it - so you can see exactly which step answers 500.
  A test text that fails with 5xx while "typing" is retried once without it, and a line is never tested against its own number. Server side:
  `docker compose logs --tail=80 evolution-api` (the stack trace) and `docker compose logs --tail=50 app` (the raw answer is logged as "Evolution API HTTP 500 on ...").
- **“Connection Closed” + `conflict ... replaced` in the Evolution log** means the *same WhatsApp session is connected twice* (two Evolution containers, a copied database, or one
  login linked in two instances) and the two keep kicking each other out; every send in that moment fails with HTTP 500. Fix: run only ONE Evolution stack
  (`docker compose ps`, `docker ps -a | grep evolution`), then `docker compose restart evolution-api`; if it keeps looping, remove the line, delete the stale
  \"Evolution API\" devices under WhatsApp > Linked devices and link the phone again. The app now waits a few seconds and retries a test text once after "Connection Closed",
  and the Lines page explains the usual errors next to the failing line.
- **Profile:** when a line connects it automatically gets the WhatsApp profile name **the gate mta** and the Gate logo as its picture (name, picture and the on/off switch are on the
  Settings page; *Apply profile* / *Apply profile to all connected lines* on the Lines page re-apply them).
- With fewer than `MIN_WORKING_LINES` (3) working lines, admins see a red alert on the home page.

**Looking less like a bot**: every ticket text is assembled from interchangeable parts (greeting, wording, optional date line, closing,
emoji; English or Hebrew via `WHATSAPP_MESSAGE_LANG`) so no two messages are the same; the gap between messages is `WHATSAPP_MIN_INTERVAL`
+ random jitter (+ the occasional longer pause), Evolution shows "typing..." for a random 1-4 s before each message, and each number is
capped per day. WhatsApp's terms do not allow unofficial clients - the durable fix is the official WhatsApp Business Platform.

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
- Door QR codes link to one ZIP (`/tickets/bundle/{token}/tickets.zip`); individual PDFs stay reachable at `/tickets/claim/{token}/ticket.pdf`. Tokens are unguessable 32-hex UUID4s.
  `/tickets/claim/{token}` is a small mobile page with a Download button.
- Ticket PDFs are additive: each upload adds pages to the pool; tick *Start over* to replace everything.
- Team and arena lists live in `app/data.py`.
- One uvicorn worker (SQLite).
