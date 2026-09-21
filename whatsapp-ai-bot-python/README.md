# WhatsApp AI Bot — Python edition

Multi-account WhatsApp AI chatbot dashboard. Originally 100% Node.js; now
**~95% Python**, with one small Node.js piece kept for a technical reason
explained below.

## Why is there still a `bridge/` folder in Node.js?

The original project used [Baileys](https://github.com/WhiskeySockets/Baileys)
to talk to WhatsApp. Baileys isn't a simple REST wrapper — it's a from-scratch
reimplementation of WhatsApp's multi-device protocol (the Noise handshake,
Signal/Sender-Key encryption, the whole thing). As of today there is no
maintained, production-ready **pure Python** equivalent of this protocol.
(The closest Python-adjacent option, `whatsmeow`, is itself written in Go —
every "Python" WhatsApp tool you'll find is actually a thin Python layer on
top of a Go or Node process doing the real protocol work.)

So this project keeps that one piece — `bridge/index.js` — as a **thin,
dumb connector**: it opens the WhatsApp session, shows the QR code, and
forwards every event (new message, connection status, QR code) to the
Python app over HTTP webhooks. It holds no business logic, no database,
and no AI calls. **Everything else is Python:**

```
┌─────────────────────┐  webhooks (HTTP)   ┌──────────────────────────┐
│  bridge/ (Node.js)   │ ─────────────────► │  server/ (Python)        │
│  Baileys connection  │ ◄───────────────── │  FastAPI + Socket.IO     │
│  QR / send / receive │   HTTP commands     │  MySQL, OpenAI, Calendar │
└─────────────────────┘                     │  dashboard + live UI    │
                                             └──────────────────────────┘
```

| Piece | Language | Responsibility |
|---|---|---|
| `bridge/` | Node.js | WhatsApp protocol only (Baileys) |
| `server/` | **Python** (FastAPI) | Dashboard API, MySQL, OpenAI replies, Google Calendar booking, Socket.IO live updates |
| `public/` | HTML/JS | Dashboard frontend (unchanged) |

## ⚠️ About your uploaded project — please rotate these

Your original zip contained a **live OpenAI API key** in `.env`, and a
**real, already-linked WhatsApp session** in `auth_sessions/` (actual
Signal-protocol session keys for a real number). Neither was carried over
into this project. Please:

1. Revoke/rotate that OpenAI key from the OpenAI dashboard.
2. Unlink and re-link the WhatsApp account (via the dashboard's "×" button,
   then scan a fresh QR) so the old session credentials stop being valid.

## Setup

### 1. Bridge (Node.js)
```bash
cd bridge
npm install
cp .env.example .env
npm start
```

### 2. Python app
```bash
cd .. # project root
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your real DeepSeek key, MySQL creds, Google OAuth creds
uvicorn server.main:app --app-dir . --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` — same dashboard UI as before. Click
"+" to link a WhatsApp number (QR appears via the bridge → Python →
Socket.IO → browser).

## Google Calendar refresh token

Same as before — see Google's
[OAuth Playground](https://developers.google.com/oauthplayground) flow:
enable the Calendar API on a Google Cloud project, authorize the
`https://www.googleapis.com/auth/calendar` scope, and copy the refresh
token into `GOOGLE_REFRESH_TOKEN`.

## Sending the first message (business-initiated / cold outreach)

Every other flow in this app starts from an *inbound* WhatsApp message. To
have the app send the very first message to someone who hasn't messaged in
yet (starting a brand-new thread), use:

```
POST /api/send
Content-Type: application/json

{
  "account_id": "<your WhatsApp account id>",
  "jid": "919876543210@s.whatsapp.net",
  "text": "Hi! Following up on your enquiry — when's a good time to talk?",
  "name": "Optional contact name"
}
```

This creates the contact/thread if it doesn't exist yet, sends it through
the Baileys bridge, saves it, and responds with both ids:

```json
{ "thread_id": 42, "message_id": 137 }
```

- `thread_id` = `contacts.id` — pass this to `GET /api/contacts/{thread_id}/messages`
  to pull the whole conversation with that number.
- `message_id` = `messages.id` — identifies this specific message row.

This send is treated like a manual/human message (not an AI reply), so the
AI won't also try to reply to it.

## Replies are now handled by an external system, not this app's AI

This app no longer auto-generates or sends its own AI replies. Every
inbound 1:1 WhatsApp message — no keyword/relevance filtering, every single
one — is forwarded to an external automation system (e.g. n8n) as soon as
it arrives:

```
POST EXTERNAL_REPLY_WEBHOOK_URL   (default: http://localhost:5678/webhook/sendMessage)
Content-Type: application/json

{
  "thread_id": 42,
  "phone_number": "918957394688",
  "jid": "918957394688@s.whatsapp.net",
  "account_id": "acc1",
  "message_id": 137,
  "message": "Mujhe website banwani hai",
  "contact_name": "Optional contact name or null"
}
```

Set `EXTERNAL_REPLY_WEBHOOK_URL` in `.env` to point at your n8n (or other)
webhook. That system should:
1. Optionally pull the rest of the thread with `GET /api/contacts/{thread_id}/messages`.
2. Decide what (if anything) to reply.
3. Send the reply back out through `POST /api/send` on this app (see the
   "Sending the first message" section above) — that's the only way
   messages actually go out to WhatsApp, since the bridge/Baileys
   connection lives here.

This forwarding only fires for 1:1 chats (`isGroup: false`); group messages
are still skipped, same as before. The original keyword/AI-classification
auto-reply code is still in `server/ai_replier.py`, just no longer called
from the webhook — safe to wire back in later if you ever want it again.

### About `phone_number` and WhatsApp's `@lid` identifiers

WhatsApp is rolling out `@lid` ("Linked Identity") JIDs for some contacts —
an opaque per-user id that hides their real phone number for privacy,
instead of the classic `<phone>@s.whatsapp.net` format. When that happens:

- `jid` in the forwarded payload will look like `123456789012345@lid`.
- The bridge tries to resolve the real phone-number JID two ways, in
  order: (1) `remoteJidAlt` on that exact message, if WhatsApp sent it;
  (2) Baileys' own `lidMapping` store, which it builds up from other
  events too (contacts sync, group participant lists, earlier messages
  from that same user) — so it can often resolve a LID even when the
  message itself didn't carry `remoteJidAlt`. Whichever succeeds is sent
  as `phoneJid`, and `phone_number` is derived from that when available.
- If neither resolves — WhatsApp has genuinely never told Baileys this
  mapping through any channel — there is no way to reverse a LID back to
  a phone number; that direction isn't supported by WhatsApp itself.
  `phone_number` then falls back to the LID's digits and `phone_is_lid:
  true` is set in the payload — treat that as an opaque id, not a
  dialable number, in your n8n workflow.

### Fixed: message text getting truncated/garbled (emoji, etc.)

The DB connection and all tables now explicitly use `utf8mb4` (MySQL's full
Unicode charset, needed for emoji and some Indic characters). If you saw a
"Data truncated for column 'body'" warning before this fix, `init_db()`
now also converts existing tables to `utf8mb4` automatically on startup —
no manual SQL needed, just restart the app once.

## Notes on the conversion

- All REST endpoints (`/api/accounts`, `/api/accounts/:id/contacts`,
  `/api/contacts/:id/messages`, `/api/contacts/all`) and Socket.IO event
  names (`qr:<id>`, `status:<id>`, `message:<id>`, `accounts:updated`) are
  unchanged, so the original `public/index.html` works with zero edits.
- The AI tool-calling flow (`schedule_meeting` → Google Calendar) is
  reproduced exactly in `server/ai_replier.py`, using the OpenAI Python SDK
  pointed at DeepSeek's OpenAI-compatible endpoint (`DEEPSEEK_BASE_URL`).
- Before replying in a 1:1 chat, `is_relevant_topic()` (in
  `server/ai_replier.py`) checks whether **that specific incoming
  message** is about a website/software/app project — keyword check
  first, with a DeepSeek classification fallback for phrasing the
  keywords miss. If it's not relevant, `webhook_message()` in
  `server/main.py` returns immediately and the AI sends nothing at
  all — no redirect message, no reply. This applies whether it's a
  brand-new conversation, or a detour off-topic in the middle of an
  otherwise relevant, ongoing conversation. Only messages that are
  genuinely about the business (or a short reply that only makes sense
  as a continuation of an on-topic discussion, e.g. "e-commerce" or
  "3 PM tomorrow") trigger an AI reply.
- `AI_SYSTEM_PROMPT` in `.env` / `.env.example` is now built directly from
  your AI Persona & Behavior Specification (identity, personality, target
  services, the Understand → Qualify → Inform → Guide → Convert flow,
  language matching, message style, no-pressure sales/pricing rules,
  never-fabricate rules, meeting-confirmation rules, and privacy/security
  boundaries). Edit it there if your services, tone, or boundaries change —
  no code changes needed.
- MySQL schema is identical; `server/db.py` uses `aiomysql` instead of
  `mysql2`.
