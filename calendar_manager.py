from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytz
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from scraper import DEFAULT_CREDENTIALS_PATH, SCOPES

load_dotenv(Path(__file__).parent / ".env")

TZ_NAME = os.getenv("TZ", "America/Sao_Paulo")
TZ = pytz.timezone(TZ_NAME)
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary").strip() or "primary"
BROKER_EMAIL = os.getenv("BROKER_EMAIL_FOR_CALENDAR", "").strip()


class CalendarManager:
    def __init__(
        self,
        credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
        calendar_id: str = CALENDAR_ID,
        broker_email: str = BROKER_EMAIL,
    ) -> None:
        self.credentials_path = credentials_path
        self.calendar_id = calendar_id
        self.broker_email = broker_email
        self._service = None

    @property
    def service(self):
        if self._service is None:
            credentials = Credentials.from_service_account_file(self.credentials_path, scopes=SCOPES)
            self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    def create_event(
        self,
        start: datetime,
        lead_name: str,
        lead_email: str,
        duration_minutes: int = 45,
        summary: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        if start.tzinfo is None:
            start = TZ.localize(start)
        end = start + timedelta(minutes=duration_minutes)

        attendees = []
        if lead_email:
            attendees.append({"email": lead_email, "displayName": lead_name})
        if self.broker_email:
            attendees.append({"email": self.broker_email, "organizer": True})

        body: dict[str, Any] = {
            "summary": summary or f"Reunião com {lead_name}",
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": TZ_NAME},
            "end": {"dateTime": end.isoformat(), "timeZone": TZ_NAME},
            "attendees": attendees,
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email", "minutes": 60},
                    {"method": "popup", "minutes": 15},
                ],
            },
            "conferenceData": {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }

        event = self.service.events().insert(
            calendarId=self.calendar_id,
            body=body,
            sendUpdates="all",
            conferenceDataVersion=1,
        ).execute()
        return event

    def list_upcoming(self, max_results: int = 10) -> list[dict[str, Any]]:
        now = datetime.now(TZ).isoformat()
        result = self.service.events().list(
            calendarId=self.calendar_id,
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])
