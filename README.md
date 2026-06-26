# SmartBox

Local email service for development and testing. **Stalwart** stores mail persistently on disk and serves plaintext SMTP and IMAP. **Roundcube** provides a webmail UI on the internal network.

> **Internal network only.** All connections use plaintext (no TLS). Do not expose these ports to the public internet. Outbound mail to external domains is relayed through an SMTP smarthost (e.g. Cloudflare) over TLS.

## Architecture

```
Browser / mail client
       │
       ├── Roundcube (8025) ──IMAP/SMTP──► Stalwart (RocksDB volume)
       │
       ├── SMTP (25, plaintext, AUTH) ────► Stalwart
       │
       └── IMAP (1143, plaintext) ◄─────── Stalwart
```

| Service | Image | Purpose |
|---------|-------|---------|
| `stalwart` | [Stalwart v0.16](https://hub.docker.com/r/stalwartlabs/stalwart) | Persistent SMTP + IMAP mail server |
| `roundcube` | [roundcube/roundcubemail](https://hub.docker.com/r/roundcube/roundcubemail) | Webmail UI (connects to Stalwart over the Docker network) |

Mail is stored in the Docker volume `smartbox-stalwart-data` (RocksDB). It survives container restarts and rebuilds.

## Quick start

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env if you want a different mailbox address or ports
```

### 2. Start services

```bash
docker compose up --build -d
```

First boot runs an automatic Stalwart bootstrap (domain, plaintext listeners, mailbox, outbound relay). This can take up to a minute.

### 3. Webmail and admin

- Roundcube webmail: http://localhost:8025
- Stalwart admin: http://localhost:8087/admin

Log in to Roundcube with your mailbox credentials (`MAIL_USER` or full `MAIL_ADDRESS`, plus `MAIL_PASSWORD`). If `MAIL_DOMAIN` is set, you can use just the local part (e.g. `default`).

## Configuration

Default mailbox (set in `.env`):

```
MAIL_ADDRESS=default@mail.thm64.com
MAIL_USER=default
MAIL_PASSWORD=smartbox-dev
MAIL_DOMAIN=mail.thm64.com
MAIL_HOSTNAME=mail.thm64.com
```

| Variable | Default | Description |
|----------|---------|-------------|
| `MAIL_ADDRESS` | `default@mail.thm64.com` | Primary mailbox address |
| `MAIL_USER` | `default` | Local part of the mailbox |
| `MAIL_PASSWORD` | `smartbox-dev` | IMAP/SMTP password for the mailbox |
| `MAIL_DOMAIN` | `mail.thm64.com` | Domain served by Stalwart |
| `MAIL_HOSTNAME` | `mail.thm64.com` | Server hostname |
| `STALWART_ADMIN_PASSWORD` | `smartbox-admin` | One-time bootstrap admin password |
| `SMTP_HOST_PORT` | `25` | Host port for SMTP (plaintext, AUTH for relay) |
| `IMAP_HOST_PORT` | `1143` | Host port for plaintext IMAP |
| `ROUNDCUBE_HOST_PORT` | `8025` | Host port for Roundcube webmail |
| `STALWART_ADMIN_HOST_PORT` | `8087` | Host port for Stalwart admin UI |
| `MAIL_SERVER_IP` | — | LAN IP used by the helper scripts |
| `SMTP_RELAY_HOST` | — | Outbound smarthost; empty disables relaying |

## Connecting from your machine

All connections are plaintext — only use this from a trusted internal network.

| Protocol | Host | Port | Username | Password | TLS |
|----------|------|------|----------|----------|-----|
| SMTP (send/relay) | server IP | `25` | `MAIL_ADDRESS` | `MAIL_PASSWORD` | none |
| SMTP (inbound MX) | server IP | `25` | — | — | none |
| IMAP | server IP | `1143` | `MAIL_ADDRESS` | `MAIL_PASSWORD` | none |

Authenticated SMTP on port 25 is required to relay to external domains; unauthenticated inbound mail can only be delivered to local mailboxes.

### Roundcube SMTP 550 ("Relay not allowed")

Stalwart returns `550 5.1.2 Relay not allowed` when mail to an external address is sent **without SMTP AUTH** and the client is not on a private network. SmartBox allows relay from RFC1918/docker addresses (e.g. `192.168.x.x`) so Roundcube can submit mail on the internal Docker network.

From-address checks are disabled (`mustMatchSender`, `isSenderAllowed`). Any `From:` header is accepted.

After updating, redeploy:

```bash
docker compose up -d --build
docker compose restart roundcube
```

If outbound mail still fails after auth works, check that `SMTP_RELAY_HOST` and `SMTP_RELAY_PASSWORD` are set in Portainer/.env. To force relay reconfiguration, remove the marker and restart:

```bash
docker compose exec stalwart rm -f /var/lib/stalwart/.smartbox-relay-v1
docker compose restart stalwart
```

### Apple 邮件 (Mail.app)

In **邮件** → **设置** → **账户**:

- Incoming: IMAP, host `192.168.1.34`, port **1143**, SSL **off**
- Outgoing: SMTP, host `192.168.1.34`, port **25**, SSL **off**, authentication **on**
- Username: full address `default@mail.thm64.com`

### List mail from the command line

```bash
python3 scripts/list_imap.py
```

## Persistence

Mail lives in the named volume `smartbox-stalwart-data`. To wipe all mail and re-run bootstrap:

```bash
docker compose down
docker volume rm smartbox-stalwart-data
docker compose up --build -d
```

## Project layout

```
SmartBox/
├── docker-compose.yml
├── .env.example
├── config/
│   ├── roundcube/custom.inc.php   # SMTP auth + SmartBox webmail tweaks
│   └── stalwart/config.json       # baked into the stalwart image at build time
├── scripts/
│   ├── list_imap.py
│   ├── set_password.py
│   └── copy_imap_to_smartbox.py
└── services/
    └── stalwart/
```

## Stopping

```bash
docker compose down
```

Use `docker compose down -v` only if you intentionally want to delete stored mail.
