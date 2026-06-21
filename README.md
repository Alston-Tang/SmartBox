# SmartBox

Local email service for development and testing. **Stalwart** stores mail persistently on disk; the **mail-ui** app provides a quick inbox view and SMTP send test page.

> **Internal network only.** All connections use plaintext (no TLS). Do not expose these ports to the public internet. Outbound mail to external domains is relayed through an SMTP smarthost (e.g. Cloudflare) over TLS.

## Architecture

```
Your app / mail client
       │
       ├── SMTP (25, plaintext, AUTH) ──► Stalwart (RocksDB volume)
       │                                         │
       └── IMAP (1143, plaintext) ◄──────────────┤
                                                 │
                                                 ▼
                                          Mail UI (8025)
                                          inbox + send test
```

| Service | Image | Purpose |
|---------|-------|---------|
| `stalwart` | [Stalwart v0.16](https://hub.docker.com/r/stalwartlabs/stalwart) | Persistent SMTP + IMAP mail server |
| `mail-ui` | Custom Flask app | Web inbox dashboard and SMTP send test page |

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

### 3. Open the web UI

- Inbox: http://localhost:8025
- Send test: http://localhost:8025/send
- Stalwart admin (optional): http://localhost:8081/admin

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
| `MAIL_ADDRESS` | `default@mail.thm64.com` | Primary mailbox; default From/To on the send test page |
| `MAIL_USER` | `default` | Local part of the mailbox |
| `MAIL_PASSWORD` | `smartbox-dev` | IMAP/SMTP password for the mailbox |
| `MAIL_DOMAIN` | `mail.thm64.com` | Domain served by Stalwart |
| `MAIL_HOSTNAME` | `mail.thm64.com` | Server hostname |
| `STALWART_ADMIN_PASSWORD` | `smartbox-admin` | One-time bootstrap admin password |
| `SMTP_HOST_PORT` | `25` | Host port for SMTP (plaintext, AUTH for relay) |
| `IMAP_HOST_PORT` | `1143` | Host port for plaintext IMAP |
| `MAIL_UI_HOST_PORT` | `8025` | Host port for the web UI |
| `STALWART_ADMIN_HOST_PORT` | `8081` | Host port for Stalwart admin UI |
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
│   └── stalwart/config.json   # baked into the stalwart image at build time
├── scripts/list_imap.py
└── services/
    ├── stalwart/
    └── mail-ui/
```

## Stopping

```bash
docker compose down
```

Use `docker compose down -v` only if you intentionally want to delete stored mail.
