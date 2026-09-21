"""
Main Python app: dashboard REST API + Socket.IO (live updates) + the
webhook receiver that the Node/Baileys bridge posts events to.

This replaces server/index.js and all the business logic that used to
live in sessionManager.js. The bridge (bridge/index.js) only knows about
the WhatsApp protocol; everything else — the database, the AI replier,
Google Calendar, and the dashboard's live updates — lives here.
"""
import asyncio
import os
import uuid
from pathlib import Path

import socketio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

load_dotenv()

from server import db
from server import bridge_client
from server.external_webhook import forward_inbound_message

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
api = FastAPI()


# ---------------------------------------------------------------------------
# REST API — same routes/shapes as the original Node app
# ---------------------------------------------------------------------------


@api.get("/api/accounts")
async def list_accounts():
    return await db.query(
        "SELECT id, phone_number, name, status, created_at FROM accounts ORDER BY created_at ASC"
    )


@api.post("/api/accounts")
async def add_account():
    """Starts linking a new WhatsApp account. Nothing is written to the DB
    here — the account only becomes real once WhatsApp actually confirms
    the pairing (see the /webhook/status handler below)."""
    account_id = str(uuid.uuid4())
    await bridge_client.start_session(account_id)
    return {"accountId": account_id}


@api.delete("/api/accounts/{account_id}")
async def remove_one_account(account_id: str):
    await bridge_client.remove_session(account_id)
    await db.delete_account(account_id)
    await sio.emit("accounts:updated")
    return {"removed": account_id}


@api.delete("/api/accounts")
async def remove_all_accounts():
    rows = await db.query("SELECT id FROM accounts")
    for row in rows:
        await bridge_client.remove_session(row["id"])
        await db.delete_account(row["id"])
    await sio.emit("accounts:updated")
    return {"removed": len(rows)}


@api.get("/api/accounts/{account_id}/contacts")
async def account_contacts(account_id: str):
    return await db.query(
        """
        SELECT c.id, c.jid, c.name,
          (SELECT MAX(m.created_at) FROM messages m WHERE m.contact_id = c.id) AS last_message_at
        FROM contacts c
        WHERE c.account_id = %s
        ORDER BY COALESCE(last_message_at, c.created_at) DESC
        """,
        (account_id,),
    )


@api.get("/api/contacts/{contact_id}/messages")
async def contact_messages(contact_id: int):
    return await db.query(
        "SELECT id, direction, body, ai_generated, created_at FROM messages "
        "WHERE contact_id = %s ORDER BY id ASC",
        (contact_id,),
    )


@api.post("/api/send")
async def send_message_api(payload: dict):
    """
    Business-initiated (first-touch) send: posts a message to WhatsApp even
    if this contact has never messaged in before. Creates the thread
    (contacts row) if it doesn't exist yet, and returns BOTH ids:
      - thread_id  -> contacts.id, identifies the whole conversation
      - message_id -> messages.id, identifies this one message
    Body: { "account_id": str, "jid": str, "text": str, "name"?: str }
    "jid" is the recipient's WhatsApp id, e.g. "919876543210@s.whatsapp.net".
    """
    account_id = payload.get("account_id")
    jid = payload.get("jid")
    text = payload.get("text")
    name = payload.get("name")

    if not account_id or not jid or not text:
        raise HTTPException(status_code=400, detail="account_id, jid and text are required")

    # Creates the contact/thread now if this is genuinely the first time
    # we're talking to this number — same call the webhook uses, so an
    # existing thread is reused instead of duplicated.
    contact_id = await db.upsert_contact(account_id, jid, name)

    sent = await bridge_client.send_message(account_id, jid, text)
    if not sent:
        raise HTTPException(status_code=502, detail="WhatsApp send failed (bridge/session issue)")

    message_id = await db.save_message(account_id, contact_id, "out", text, False)
    # Business-initiated, not the AI — mark it like any manual/human send so
    # the AI doesn't also jump in and reply to its own outbound message.
    await db.mark_human_reply(contact_id)

    await sio.emit(
        f"message:{account_id}",
        {"contactId": contact_id, "jid": jid, "direction": "out", "body": text, "aiGenerated": False},
    )
    await sio.emit("accounts:updated")

    return {"thread_id": contact_id, "message_id": message_id}


@api.get("/api/contacts/all")
async def all_contacts():
    return await db.query(
        """
        SELECT c.id, c.jid, c.name, c.account_id, a.phone_number, a.name AS account_name,
          (SELECT MAX(m.created_at) FROM messages m WHERE m.contact_id = c.id) AS last_message_at
        FROM contacts c
        JOIN accounts a ON a.id = c.account_id
        ORDER BY COALESCE(last_message_at, c.created_at) DESC
        """
    )


# ---------------------------------------------------------------------------
# Webhooks — the bridge posts every WhatsApp event here
# ---------------------------------------------------------------------------


@api.post("/webhook/qr")
async def webhook_qr(payload: dict):
    await sio.emit(f"qr:{payload['accountId']}", {"dataUrl": payload["dataUrl"]})
    return {"ok": True}


@api.post("/webhook/status")
async def webhook_status(payload: dict):
    account_id = payload["accountId"]
    status = payload["status"]

    if status == "connected":
        await db.upsert_account_connected(account_id, payload.get("phone"), payload.get("name"))
        await sio.emit(f"status:{account_id}", {"status": "connected", "phone": payload.get("phone")})
        await sio.emit("accounts:updated")
    elif status == "pending_qr":
        await db.update_account_status(account_id, "pending_qr")
        await sio.emit(f"status:{account_id}", {"status": "pending_qr"})
    elif status == "disconnected":
        await db.update_account_status(account_id, "disconnected")
        await sio.emit(f"status:{account_id}", {"status": "disconnected"})
        await sio.emit("accounts:updated")
    # "reconnecting" is a transient state (e.g. Baileys' 515 "restart
    # required" right after a QR scan) — deliberately not persisted.

    return {"ok": True}


# NOTE: the built-in AI auto-reply (_reply_after_delay -> generate_reply in
# server/ai_replier.py) has been retired from this webhook path per your
# request — every inbound message is now forwarded to the external reply
# system instead (see forward_inbound_message above). server/ai_replier.py
# itself is left untouched/unused in case you want to switch back later.


@api.post("/webhook/message")
async def webhook_message(payload: dict):
    account_id = payload["accountId"]
    jid = payload["jid"]
    is_group = payload.get("isGroup", False)
    body = payload["body"]
    from_me = payload.get("fromMe", False)

    contact_id = await db.upsert_contact(account_id, jid, payload.get("name"))

    # ------------------------------------------------------------------
    # Case A: the account holder typed this themselves (their own phone /
    # WhatsApp Web) — save it, pause the AI for this chat, and stop here.
    # ------------------------------------------------------------------
    if from_me:
        await db.save_message(account_id, contact_id, "out", body, False)
        await db.mark_human_reply(contact_id)
        await sio.emit(
            f"message:{account_id}",
            {"contactId": contact_id, "jid": jid, "direction": "out", "body": body, "aiGenerated": False},
        )
        await sio.emit("accounts:updated")
        return {"ok": True}

    # ------------------------------------------------------------------
    # Case B: an inbound message from the recipient.
    # ------------------------------------------------------------------
    message_id = await db.save_message(account_id, contact_id, "in", body, False)
    await sio.emit(
        f"message:{account_id}",
        {"contactId": contact_id, "jid": jid, "direction": "in", "body": body, "aiGenerated": False},
    )
    await sio.emit("accounts:updated")

    # Groups have multiple participants — forwarding those to the external
    # reply system would be spammy/ambiguous (whose phone number would it
    # even be?), so only 1:1 chats get forwarded, same as before.
    if is_group:
        return {"ok": True}

    # No keyword/relevance filtering here on purpose: EVERY inbound 1:1
    # message gets its thread_id (contacts.id) + phone_number forwarded to
    # the external reply system. That system now owns the "should we
    # reply, and with what" decision — this app no longer generates or
    # sends its own AI reply. Fire-and-forget so a slow/down external
    # system never blocks or breaks the webhook response to the bridge.
    #
    # WhatsApp's newer @lid privacy identifiers mean `jid` isn't always
    # phone-number-based. The bridge sends `phoneJid` (from Baileys'
    # remoteJidAlt) when it has the real phone-number JID for this
    # contact — prefer that; otherwise fall back to the LID's digits and
    # flag phone_is_lid so the external system knows it's not dialable.
    phone_jid = payload.get("phoneJid")
    if phone_jid and phone_jid.endswith("@s.whatsapp.net"):
        phone_number = phone_jid.split("@")[0]
        phone_is_lid = False
    else:
        phone_number = jid.split("@")[0]
        phone_is_lid = not jid.endswith("@s.whatsapp.net")

    asyncio.create_task(
        forward_inbound_message(
            thread_id=contact_id,
            phone_number=phone_number,
            jid=jid,
            account_id=account_id,
            message_id=message_id,
            message=body,
            contact_name=payload.get("name"),
            phone_is_lid=phone_is_lid,
        )
    )

    return {"ok": True}


@api.post("/webhook/history")
async def webhook_history(payload: dict):
    account_id = payload["accountId"]
    # History can arrive before the "connected" webhook finishes — receiving
    # it at all is itself proof the pairing succeeded.
    await db.upsert_account_connected(account_id, payload.get("phone"), payload.get("name"))

    imported = False
    for item in payload.get("items", []):
        contact_id = await db.upsert_contact(account_id, item["jid"], item.get("name"))
        await db.save_message(account_id, contact_id, item["direction"], item["body"], False)
        imported = True

    if imported:
        await sio.emit("accounts:updated")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Startup + static frontend (same public/index.html, unchanged)
# ---------------------------------------------------------------------------


@api.on_event("startup")
async def on_startup():
    await db.init_db()


api.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")

app = socketio.ASGIApp(sio, other_asgi_app=api)
