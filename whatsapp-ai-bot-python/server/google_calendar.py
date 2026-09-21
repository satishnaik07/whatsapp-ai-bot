"""
Google Calendar booking, same OAuth2 refresh-token flow as the original.
"""
import os
import uuid
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _get_credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri=TOKEN_URI,
    )


def create_event(title: str, start_iso: str, end_iso: str, attendee_email: str | None = None) -> dict:
    """Books a meeting on Google Calendar, with a Google Meet video-call
    link attached. Returns {"id", "html_link", "meet_link"} — meet_link is
    the joinable https://meet.google.com/... URL (falls back to None if
    Google didn't attach one for some reason, e.g. no Meet-enabled
    Workspace on the calendar's account)."""
    creds = _get_credentials()
    service = build("calendar", "v3", credentials=creds)

    event_body = {
        "summary": title,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    if attendee_email:
        event_body["attendees"] = [{"email": attendee_email}]

    event = (
        service.events()
        .insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event_body,
            sendUpdates="all" if attendee_email else "none",
            conferenceDataVersion=1,
        )
        .execute()
    )
    return {
        "id": event["id"],
        "html_link": event.get("htmlLink"),
        "meet_link": event.get("hangoutLink"),
    }
