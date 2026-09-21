"""
Generates the AI's WhatsApp reply, including calling Google Calendar via
a tool call when the customer agrees to a meeting time. Mirrors the
original aiReplier.js flow. Uses DeepSeek's chat-completions API, which
is OpenAI-compatible, so we just point the OpenAI SDK at DeepSeek's
base_url instead of switching SDKs.
"""
import asyncio
import json
import os
import re

from openai import AsyncOpenAI

from server.db import query, execute
from server.google_calendar import create_event

openai_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# ---------------------------------------------------------------------------
# Topic filter — only auto-reply when the conversation is actually about
# building a website/software/app, so the bot doesn't jump into every
# random chat. Keyword pre-filter (cheap, catches the obvious cases) with
# an optional AI classification fallback for phrasing the keywords miss.
# ---------------------------------------------------------------------------

TOPIC_KEYWORDS = [
    "website", "web site", "webpage", "web app", "web application",
    "software", "app develop", "app banwana", "mobile app", "android app",
    "ios app", "e-commerce", "ecommerce", "online store", "portfolio site",
    "landing page", "saas", "developer chahiye", "coding", "programming",
    "system develop", "crm", "erp", "automation tool", "bot banwana",
    "website banwana", "site banwana", "software banwana", "app banao",
    "website chahiye", "developer", "tech stack", "database design",
]

_KEYWORD_PATTERN = re.compile(
    r"(" + "|".join(re.escape(k) for k in TOPIC_KEYWORDS) + r")", re.IGNORECASE
)


def _keyword_hit(text: str) -> bool:
    return bool(_KEYWORD_PATTERN.search(text or ""))


async def is_relevant_topic(incoming_text: str, history: list[dict] | None = None) -> bool:
    """Returns True only if the CURRENT incoming message is on-topic — a
    website/software/app request, or a short reply that only makes sense
    as a continuation of an ongoing one (e.g. "e-commerce", "100
    products", "3 PM tomorrow"). History is used purely to interpret such
    short/ambiguous replies; it does NOT make an unrelated message
    relevant just because the chat touched on the topic earlier. That
    way, if someone drifts off-topic mid-conversation, that specific
    message is correctly treated as irrelevant and the AI stays quiet.
    Cheap keyword check first; if that's inconclusive, ask the model to
    classify using a bit of recent context."""
    if _keyword_hit(incoming_text):
        return True

    # Fallback: ask the model, since keywords miss things like "I need
    # someone to build this for me" with no obvious trigger word, and
    # short continuation replies like "around 100" that only make sense
    # in context.
    try:
        context = "\n".join(f"{m['role']}: {m['content']}" for m in (history or [])[-6:])
        prompt = (
            "Conversation so far (for context only):\n"
            f"{context}\n\n"
            f'Latest message from the customer: "{incoming_text}"\n\n'
            "Decide ONLY about the latest message above, not the whole "
            "conversation. Answer YES if the latest message is:\n"
            "(a) directly about building/hiring for a website, app, or "
            "software project, OR\n"
            "(b) a short reply that only makes sense as a continuation of "
            "an ongoing website/app/software discussion above (e.g. "
            "answering 'how many products', confirming a meeting time, "
            "giving a budget or timeline).\n"
            "Answer NO if the latest message is a new, unrelated topic — "
            "even if earlier messages in the conversation were about a "
            "website/software project.\n"
            "Reply with only YES or NO."
        )
        resp = await openai_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3,
            temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip().upper()
        return answer.startswith("Y")
    except Exception as err:  # noqa: BLE001
        print(f"[ai_replier] topic classification failed, defaulting to skip: {err}")
        return False


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": "Book a meeting on Google Calendar once the customer has agreed on a date/time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short meeting title"},
                    "start_iso": {
                        "type": "string",
                        "description": "Meeting start time in ISO 8601 with timezone offset, e.g. 2026-08-28T15:00:00+05:30",
                    },
                    "end_iso": {
                        "type": "string",
                        "description": "Meeting end time in ISO 8601 with timezone offset",
                    },
                },
                "required": ["title", "start_iso", "end_iso"],
            },
        },
    }
]


async def _load_history(contact_id: int, limit: int = 20) -> list[dict]:
    rows = await query(
        "SELECT direction, body FROM messages WHERE contact_id = %s ORDER BY id DESC LIMIT %s",
        (contact_id, limit),
    )
    rows.reverse()
    return [
        {"role": "user" if r["direction"] == "in" else "assistant", "content": r["body"]}
        for r in rows
    ]


async def generate_reply(account_id: str, contact_id: int, incoming_text: str) -> tuple[str, bool]:
    """Returns (reply_text, meeting_booked) — meeting_booked tells the
    caller whether to close out this conversation (ai_status='closed')."""
    history = await _load_history(contact_id)
    is_first_ai_turn = not any(m["role"] == "assistant" for m in history)

    base_prompt = os.getenv("AI_SYSTEM_PROMPT", "You are a helpful WhatsApp assistant.")
    style_rules = (
        "\n\n--- Reply style rules ---\n"
        "- Write like a professional running their own business on WhatsApp: "
        "polite, clear, to the point. No slang, no over-familiarity, but also "
        "not stiff or robotic.\n"
        "- Reply in the SAME language/script the other person is using — if "
        "they write in English, reply in English; if Hindi (Devanagari or "
        "Hinglish/roman), reply the same way; if Marathi, reply in Marathi. "
        "Match their language for the whole conversation, don't switch "
        "languages mid-way.\n"
        "- Keep messages short — a few lines at most, no long paragraphs, no "
        "bullet points or headings, unless the person specifically asked for "
        "detailed information.\n"
        "- Don't use stiff openers like 'Certainly!', 'I'd be happy to "
        "assist', 'Dear Sir/Madam'. Get straight to answering.\n"
        + (
            "- This is the first reply in this conversation — greet them "
            "briefly and professionally before answering (e.g. a short "
            "'Hello, thanks for reaching out' in their language), then "
            "respond to what they asked.\n"
            if is_first_ai_turn
            else "- This conversation is already ongoing — do not re-introduce "
            "yourself or greet again, just continue naturally from where it "
            "left off.\n"
        )
        + "- Never mention that you are an AI, a bot, a model, or an "
        "assistant software, and don't add disclaimers about being "
        "automated."
    )

    messages = [
        {
            "role": "system",
            "content": base_prompt + style_rules,
        },
        *history,
        {"role": "user", "content": incoming_text},
    ]

    response = await openai_client.chat.completions.create(
        model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto"
    )
    choice = response.choices[0]
    meeting_booked = False

    if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
        messages.append(choice.message.model_dump(exclude_none=True))

        for call in choice.message.tool_calls:
            if call.function.name == "schedule_meeting":
                args = json.loads(call.function.arguments)
                try:
                    # google-api-python-client is sync — run off the event loop
                    evt = await asyncio.to_thread(
                        create_event, args["title"], args["start_iso"], args["end_iso"]
                    )
                    await execute(
                        """
                        INSERT INTO meetings
                            (account_id, contact_id, title, start_time, end_time, google_event_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            account_id,
                            contact_id,
                            args["title"],
                            args["start_iso"],
                            args["end_iso"],
                            evt["id"],
                        ),
                    )
                    tool_result = {"success": True, "event_link": evt["html_link"]}
                    meeting_booked = True
                except Exception as err:  # noqa: BLE001
                    print(f"[ai_replier] calendar booking failed: {err}")
                    tool_result = {
                        "success": False,
                        "error": "Could not book the meeting, please suggest another time.",
                    }

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(tool_result),
                    }
                )

        response = await openai_client.chat.completions.create(model=MODEL, messages=messages)
        choice = response.choices[0]

    content = choice.message.content
    reply = content.strip() if content else "Sorry, I didn't catch that — could you repeat it?"
    return reply, meeting_booked
