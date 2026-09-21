"""
Forwards every inbound WhatsApp message to an external automation system
(e.g. n8n) instead of this app's own AI generating a reply.

That external system is now the one deciding what (if anything) to reply
with. It receives the thread_id (contacts.id) and phone_number for every
single inbound message — no keyword/relevance filtering here, unlike the
old built-in AI reply path — and can look the rest of the conversation up
via this app's own API (GET /api/contacts/{thread_id}/messages) and send
its reply back out through POST /api/send.
"""
import os
import httpx

EXTERNAL_REPLY_WEBHOOK_URL = os.getenv(
    "EXTERNAL_REPLY_WEBHOOK_URL", os.getenv("Webhook_URL")
)


async def forward_inbound_message(
    thread_id: int,
    phone_number: str,
    jid: str,
    account_id: str,
    message_id: int,
    message: str,
    contact_name: str | None = None,
    phone_is_lid: bool = False,
) -> bool:
    """POSTs the new inbound message to the external system. Fire-and-forget
    by design — logs on failure but never raises, so a slow/unreachable n8n
    never breaks message handling or the webhook response back to the
    bridge.

    phone_is_lid=True means WhatsApp didn't give us a real phone-number JID
    for this contact (it's on the newer @lid privacy identifier and Baileys
    had no phone-number mapping for it yet) — phone_number is the LID's
    digits, NOT a dialable phone number. The external system should treat
    it as an opaque id in that case, not attempt to use it as a number."""
    payload = {
        "thread_id": thread_id,
        "phone_number": phone_number,
        "phone_is_lid": phone_is_lid,
        "jid": jid,
        "account_id": account_id,
        "message_id": message_id,
        "message": message,
        "contact_name": contact_name,
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(EXTERNAL_REPLY_WEBHOOK_URL, json=payload, timeout=15)
            if resp.status_code >= 400:
                print(
                    f"[external_webhook] non-2xx from {EXTERNAL_REPLY_WEBHOOK_URL}: "
                    f"{resp.status_code} {resp.text[:300]}"
                )
                return False
            return True
        except httpx.HTTPError as e:
            print(f"[external_webhook] forward to {EXTERNAL_REPLY_WEBHOOK_URL} failed: {e}")
            return False
