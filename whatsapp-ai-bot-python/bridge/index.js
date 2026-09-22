/**
 * WhatsApp bridge (Baileys) — intentionally minimal.
 *
 * This is the ONLY piece of the app still in Node.js. Baileys implements
 * WhatsApp's multi-device protocol (Noise handshake + Signal encryption)
 * and has no maintained Python equivalent, so we keep it here as a thin
 * connector and do EVERYTHING else (dashboard, DB, AI, calendar) in Python.
 *
 * This process:
 *   - opens/maintains Baileys sessions (QR pairing, reconnects, history sync)
 *   - forwards every event to the Python app via webhook POSTs
 *   - exposes a tiny HTTP API so Python can start/stop sessions and send messages
 *
 * It holds NO business logic, NO database, and NO AI calls — Python owns all of that.
 */
require('dotenv').config();
const path = require('path');
const fs = require('fs/promises');
const express = require('express');
const QRCode = require('qrcode');
const pino = require('pino');
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
} = require('@whiskeysockets/baileys');

const AUTH_DIR = path.join(__dirname, 'auth_sessions');
const BRIDGE_PORT = process.env.BRIDGE_PORT || 4000;

// When Python sends a message through /send, WhatsApp echoes it straight
// back through messages.upsert with fromMe:true — same as a human typing
// from their own phone. We remember what the bot just sent (per chat, for
// a few seconds) so that echo isn't mistaken for a real human takeover.
const recentBotSent = new Map(); // "accountId:jid" -> Set<text>

function rememberBotSent(accountId, jid, text) {
  const key = `${accountId}:${jid}`;
  if (!recentBotSent.has(key)) recentBotSent.set(key, new Set());
  recentBotSent.get(key).add(text);
  setTimeout(() => {
    recentBotSent.get(key)?.delete(text);
  }, 30_000);
}

function wasJustSentByBot(accountId, jid, text) {
  const key = `${accountId}:${jid}`;
  const set = recentBotSent.get(key);
  if (set && set.has(text)) {
    set.delete(text);
    return true;
  }
  return false;
}

const PYTHON_WEBHOOK_URL = process.env.PYTHON_WEBHOOK_URL;

const app = express();
app.use(express.json());

const sockets = new Map(); // accountId -> live baileys socket
const groupNameCache = new Map();

async function postWebhook(pathname, payload) {
  try {
    await fetch(`${PYTHON_WEBHOOK_URL}${pathname}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    console.error(`[bridge] webhook ${pathname} failed:`, e.message);
  }
}

function extractText(msg) {
  return (
    msg.message?.conversation ||
    msg.message?.extendedTextMessage?.text ||
    msg.message?.imageMessage?.caption ||
    ''
  );
}

async function getGroupName(sock, accountId, jid, hint) {
  const cacheKey = `${accountId}:${jid}`;
  if (groupNameCache.has(cacheKey)) return groupNameCache.get(cacheKey);
  if (hint) {
    groupNameCache.set(cacheKey, hint);
    return hint;
  }
  try {
    const meta = await sock.groupMetadata(jid);
    const name = meta?.subject || jid;
    groupNameCache.set(cacheKey, name);
    return name;
  } catch (e) {
    return jid;
  }
}

/**
 * Starts (or restarts) a Baileys session for accountId. Every connection
 * event is simply forwarded to Python as a webhook — Python decides what
 * to persist (accounts/contacts/messages tables) and what to do about it
 * (e.g. calling OpenAI for a reply).
 */
async function createSession(accountId) {
  const { state, saveCreds } = await useMultiFileAuthState(path.join(AUTH_DIR, accountId));
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    browser: ['WhatsApp AI Bot', 'Chrome', '1.0.0'],
    syncFullHistory: true,
  });

  sockets.set(accountId, sock);
  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      const dataUrl = await QRCode.toDataURL(qr);
      await postWebhook('/qr', { accountId, dataUrl });
      await postWebhook('/status', { accountId, status: 'pending_qr' });
    }

    if (connection === 'open') {
      const phone = sock.user?.id?.split(':')[0] || null;
      const name = sock.user?.name || sock.user?.notify || null;
      await postWebhook('/status', { accountId, status: 'connected', phone, name });
    }

    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      console.log(
        `[bridge ${accountId}] closed (statusCode=${statusCode}, loggedOut=${loggedOut}):`,
        lastDisconnect?.error?.message || lastDisconnect?.error
      );
      sockets.delete(accountId);

      if (loggedOut) {
        await postWebhook('/status', { accountId, status: 'disconnected', loggedOut: true });
        await fs.rm(path.join(AUTH_DIR, accountId), { recursive: true, force: true }).catch(() => {});
        return;
      }

      // statusCode 515 "restart required" fires right after a QR scan and
      // just means "reconnect once more to finish pairing" — always retry
      // with the same saved creds except on an explicit logout.
      await postWebhook('/status', { accountId, status: 'reconnecting' });
      createSession(accountId).catch((e) => console.error('[bridge] reconnect failed', e));
    }
  });

  // WhatsApp pushes existing chat history right after a successful link.
  sock.ev.on('messaging-history.set', async ({ chats, messages }) => {
    try {
      const phone = sock.user?.id?.split(':')[0] || null;
      const name = sock.user?.name || sock.user?.notify || null;

      const nameByJid = new Map();
      for (const chat of chats || []) {
        if (chat?.id && chat.name) nameByJid.set(chat.id, chat.name);
      }

      const items = [];
      for (const msg of messages || []) {
        if (!msg.message) continue;
        const jid = msg.key.remoteJid;
        if (!jid || jid === 'status@broadcast') continue;
        const isGroup = jid.endsWith('@g.us');
        const text = extractText(msg);
        if (!text) continue;

        const chatName = isGroup
          ? await getGroupName(sock, accountId, jid, nameByJid.get(jid))
          : nameByJid.get(jid) || msg.pushName || null;

        items.push({
          jid,
          name: chatName,
          isGroup,
          direction: msg.key.fromMe ? 'out' : 'in',
          body: text,
        });
      }

      if (items.length) {
        await postWebhook('/history', { accountId, phone, name, items });
      }
    } catch (err) {
      console.error('[bridge] history import failed:', err.message);
    }
  });

  // Resolves a possibly-@lid jid down to its real phone-number JID.
  // Order of attempts, cheapest/most-reliable first:
  //   1. Already a phone JID — nothing to do.
  //   2. remoteJidAlt on this exact message — WhatsApp sent the mapping
  //      along with this message.
  //   3. Baileys' own lidMapping store — it learns PN<->LID pairs from
  //      OTHER events too (contacts sync, group participant lists, prior
  //      messages from this same user elsewhere), so it can resolve a LID
  //      even when this particular message didn't carry remoteJidAlt.
  // Still not guaranteed: if WhatsApp has never told Baileys this
  // mapping through ANY channel, there is no way to reverse a LID back to
  // a phone number — that direction isn't supported by WhatsApp itself.
  async function resolvePhoneJid(remoteJidAlt, jid) {
    if (jid.endsWith('@s.whatsapp.net')) return jid;
    if (remoteJidAlt) return remoteJidAlt;
    try {
      const pn = await sock.signalRepository.lidMapping.getPNForLID(jid);
      return pn || null;
    } catch (e) {
      console.error('[bridge] lidMapping lookup failed:', e.message);
      return null;
    }
  }

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify' && type !== 'append') return;

    for (const msg of messages) {
      if (!msg.message) continue;
      const jid = msg.key.remoteJid;
      if (jid === 'status@broadcast') continue;
      const isGroup = jid.endsWith('@g.us');
      const text = extractText(msg);
      if (!text) continue;

      // fromMe = the account holder typed this themselves from their own
      // phone/WhatsApp Web — Python needs to see these too, so it can tell
      // "the human took over this chat" apart from the bot's own replies.
      // But if the bot itself just sent this exact text via /send, it's
      // an echo of our own message, not a human takeover — skip it.
      const fromMe = !!msg.key.fromMe;
      if (fromMe && wasJustSentByBot(accountId, jid, text)) continue;

      // WhatsApp is rolling out @lid ("Linked Identity") JIDs that hide the
      // real phone number for privacy — `jid` above may be
      // "<opaque-id>@lid" instead of "<phone>@s.whatsapp.net". Resolve it
      // to the real phone-number JID where possible (see resolvePhoneJid
      // above) so Python doesn't treat the opaque LID digits as a number.
      const phoneJid = await resolvePhoneJid(msg.key.remoteJidAlt, jid);

      const name = isGroup ? await getGroupName(sock, accountId, jid) : msg.pushName || null;
      await postWebhook('/message', { accountId, jid, phoneJid, name, isGroup, fromMe, body: text });
    }
  });

  return sock;
}

// ---------- HTTP API for Python to control this bridge ----------

// Start (or restart) a WhatsApp session
app.post('/sessions/:accountId', async (req, res) => {
  createSession(req.params.accountId).catch((e) =>
    console.error('[bridge] createSession failed:', e.message)
  );
  res.json({ started: req.params.accountId });
});

// Send a message out through a linked session
app.post('/sessions/:accountId/send', async (req, res) => {
  const sock = sockets.get(req.params.accountId);
  if (!sock) return res.status(409).json({ error: 'session not connected' });
  const { jid, text } = req.body;
  try {
    await sock.sendMessage(jid, { text });
    rememberBotSent(req.params.accountId, jid, text);
    res.json({ sent: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Fully unlink a session: logout on WhatsApp's side + wipe local auth files
app.delete('/sessions/:accountId', async (req, res) => {
  const accountId = req.params.accountId;
  const sock = sockets.get(accountId);
  if (sock) {
    try {
      await sock.logout();
    } catch (e) {
      /* already disconnected — fine */
    }
    try {
      sock.end(undefined);
    } catch (e) {
      /* ignore */
    }
    sockets.delete(accountId);
  }
  await fs.rm(path.join(AUTH_DIR, accountId), { recursive: true, force: true }).catch(() => {});
  res.json({ removed: accountId });
});

// ---------- boot: restore any sessions that already have saved auth ----------

async function restoreAllSessions() {
  await fs.mkdir(AUTH_DIR, { recursive: true });
  const entries = await fs.readdir(AUTH_DIR, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.isDirectory()) {
      createSession(entry.name).catch((e) =>
        console.error(`[bridge] failed to restore ${entry.name}:`, e.message)
      );
    }
  }
}

restoreAllSessions()
  .then(() => {
    app.listen(BRIDGE_PORT, () =>
      console.log(`[bridge] WhatsApp connector running on http://localhost:${BRIDGE_PORT}`)
    );
  })
  .catch((err) => {
    console.error('[bridge] failed to start:', err);
    process.exit(1);
  });
