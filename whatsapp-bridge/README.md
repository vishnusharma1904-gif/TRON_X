# TRON-X WhatsApp Bridge (Baileys)

Open-source, free, fast WhatsApp send/receive for TRON-X. Talks the WhatsApp
Web **multi-device** protocol directly over a WebSocket — **no browser**, no
WhatsApp Business account, no message templates, no 24-hour window.

It runs as a tiny **localhost-only** HTTP service that the Python app calls with
a shared bearer token. Messages are end-to-end encrypted because this *is* the
WhatsApp protocol (libsignal under the hood).

## Setup

```bash
cd whatsapp-bridge
npm install                      # installs @whiskeysockets/baileys

# REQUIRED: a shared secret used by both the bridge and TRON-X
export WHATSAPP_BRIDGE_TOKEN="$(openssl rand -hex 32)"

npm start
```

On first run a QR code prints in the terminal (and is available at
`GET /qr`). Open WhatsApp on your phone → **Settings → Linked Devices → Link a
Device** and scan it. The session is saved under `auth/` so you only scan once.

## Configuration (env)

| Var | Default | Meaning |
|-----|---------|---------|
| `WHATSAPP_BRIDGE_TOKEN`   | *(required)* | Shared bearer token. The bridge refuses every request if unset. |
| `WHATSAPP_BRIDGE_HOST`    | `127.0.0.1`  | Bind address. Keep on localhost. |
| `WHATSAPP_BRIDGE_PORT`    | `8088`       | Port. |
| `WHATSAPP_BRIDGE_AUTH_DIR`| `./auth`     | Where the linked-device session is stored. |
| `WHATSAPP_INGEST_URL`     | *(empty)*    | If set, inbound messages are POSTed here (TRON-X `/api/whatsapp/bridge/ingest`) so reading works too. |

Set the **same** token in TRON-X's `.env` as `WHATSAPP_BRIDGE_TOKEN`, and set
`WHATSAPP_BACKEND=baileys`.

## HTTP API

All routes except `/health` require `Authorization: Bearer <WHATSAPP_BRIDGE_TOKEN>`.

- `GET  /health` → `{ ok, connected }` (no auth)
- `GET  /status` → `{ connected, me, hasQr, lastError }`
- `GET  /qr`     → `{ qr }` (raw string to render; 409 once linked)
- `POST /send`   → body `{ "to": "<number or jid>", "message": "<text>" }` → `{ success, id, jid }`

## Security notes

- Binds to `127.0.0.1` only — not reachable off-box.
- Constant-time token comparison; the bridge will not start serving authed
  routes with an empty token.
- `auth/` (your linked session) and `node_modules/` are git-ignored. Treat
  `auth/` like a password — anyone with it can send as you.
