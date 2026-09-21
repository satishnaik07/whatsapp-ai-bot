"""
Small HTTP client for talking to the Node/Baileys bridge (bridge/index.js).
"""
import os
import httpx

BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:4000")


async def start_session(account_id: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{BRIDGE_URL}/sessions/{account_id}", timeout=10)


async def send_message(account_id: str, jid: str, text: str) -> bool:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{BRIDGE_URL}/sessions/{account_id}/send",
                json={"jid": jid, "text": text},
                timeout=30,
            )
            return resp.status_code == 200
        except httpx.HTTPError as e:
            print(f"[bridge_client] send_message failed: {e}")
            return False


async def remove_session(account_id: str):
    async with httpx.AsyncClient() as client:
        try:
            await client.delete(f"{BRIDGE_URL}/sessions/{account_id}", timeout=15)
        except httpx.HTTPError as e:
            print(f"[bridge_client] remove_session failed: {e}")
