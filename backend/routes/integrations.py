import os
import json
import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

router = APIRouter()

# ── Config ────────────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/integrations/calendar/callback")

FRONTEND_URL         = os.getenv("FRONTEND_URL", "http://localhost:3000")

INSFORGE_BASE_URL    = os.getenv("INSFORGE_BASE_URL")
INSFORGE_SERVICE_KEY = os.getenv("INSFORGE_SERVICE_KEY")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_flow() -> Flow:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth credentials not configured on the server. "
                   "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in backend/.env"
        )
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": [GOOGLE_REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    return flow


async def _save_refresh_token(wallet: str, refresh_token: str):
    """Persist the Google refresh token to the doctor profile in InsForge."""
    if not INSFORGE_BASE_URL or not INSFORGE_SERVICE_KEY:
        # No InsForge — skip silently (token won't persist across restarts)
        return
    async with httpx.AsyncClient() as client:
        await client.patch(
            f"{INSFORGE_BASE_URL}/rest/v1/doctor_profiles?wallet_address=eq.{wallet}",
            headers={
                "apikey": INSFORGE_SERVICE_KEY,
                "Authorization": f"Bearer {INSFORGE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "google_refresh_token": refresh_token,
                "google_calendar_connected": True,
            },
        )


async def _get_refresh_token(wallet: str) -> Optional[str]:
    """Fetch the stored Google refresh token for a doctor."""
    if not INSFORGE_BASE_URL or not INSFORGE_SERVICE_KEY:
        return None
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{INSFORGE_BASE_URL}/rest/v1/doctor_profiles"
            f"?wallet_address=eq.{wallet}&select=google_refresh_token",
            headers={
                "apikey": INSFORGE_SERVICE_KEY,
                "Authorization": f"Bearer {INSFORGE_SERVICE_KEY}",
            },
        )
        data = res.json()
        if data and isinstance(data, list) and data[0].get("google_refresh_token"):
            return data[0]["google_refresh_token"]
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/integrations/calendar/auth")
async def calendar_auth(wallet: str = Query(...)):
    """
    Step 1 — Generate the Google OAuth consent URL and return it to the frontend.
    The frontend redirects the user to this URL.
    """
    flow = _get_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=wallet,          # carry wallet address through the OAuth round-trip
        include_granted_scopes="true",
    )
    return {"url": auth_url}


@router.get("/integrations/calendar/callback")
async def calendar_callback(
    code: str = Query(...),
    state: str = Query(...),   # wallet address
):
    """
    Step 2 — Google redirects here after the user grants consent.
    Exchange the auth code for tokens and persist the refresh token.
    """
    flow = _get_flow()
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        return RedirectResponse(
            url=f"{FRONTEND_URL}/doctor?calendar_error={str(e)}"
        )

    creds = flow.credentials
    if not creds.refresh_token:
        return RedirectResponse(
            url=f"{FRONTEND_URL}/doctor?calendar_error=no_refresh_token"
        )

    await _save_refresh_token(state, creds.refresh_token)
    return RedirectResponse(url=f"{FRONTEND_URL}/doctor?calendar_connected=true")


class CalendarCreateRequest(BaseModel):
    appointment_id: str
    doctor_wallet: str
    patient_wallet: Optional[str] = None
    date: str          # YYYY-MM-DD
    time: str          # HH:MM
    reason: Optional[str] = "General consultation"


@router.post("/integrations/calendar/create")
async def calendar_create(payload: CalendarCreateRequest):
    """
    Create a Google Calendar event with a Google Meet link for a confirmed appointment.
    """
    refresh_token = await _get_refresh_token(payload.doctor_wallet)
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="Doctor has not connected Google Calendar or token not found."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    try:
        service = build("calendar", "v3", credentials=creds)

        start_dt = f"{payload.date}T{payload.time}:00"
        from datetime import datetime, timedelta
        end_dt_obj = datetime.fromisoformat(start_dt) + timedelta(minutes=30)
        end_dt = end_dt_obj.isoformat()

        event_body = {
            "summary": "ClearPulse: Patient Consultation",
            "description": (
                f"Appointment via ClearPulse AI Healthcare Platform\n\n"
                f"Patient: {payload.patient_wallet or 'N/A'}\n"
                f"Reason: {payload.reason}"
            ),
            "start": {"dateTime": start_dt, "timeZone": "UTC"},
            "end":   {"dateTime": end_dt,   "timeZone": "UTC"},
            "conferenceData": {
                "createRequest": {
                    "requestId": f"clearpulse-{payload.appointment_id}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 30},
                    {"method": "email", "minutes": 60},
                ],
            },
        }

        event = service.events().insert(
            calendarId="primary",
            body=event_body,
            conferenceDataVersion=1,
        ).execute()

        meet_link = (
            next(
                (e["uri"] for e in event.get("conferenceData", {}).get("entryPoints", [])
                 if e["entryPointType"] == "video"),
                event.get("hangoutLink", ""),
            )
        )

        # Update appointment record in InsForge if available
        if INSFORGE_BASE_URL and INSFORGE_SERVICE_KEY:
            async with httpx.AsyncClient() as client:
                await client.patch(
                    f"{INSFORGE_BASE_URL}/rest/v1/appointments?id=eq.{payload.appointment_id}",
                    headers={
                        "apikey": INSFORGE_SERVICE_KEY,
                        "Authorization": f"Bearer {INSFORGE_SERVICE_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={"google_event_id": event["id"], "meeting_link": meet_link},
                )

        return {"success": True, "google_event_id": event["id"], "meeting_link": meet_link}

    except HttpError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
