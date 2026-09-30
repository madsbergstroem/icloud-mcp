"""SEQUENCE is bumped only for a significant change by the organizer (RFC 5546), never for a private edit or by an attendee."""
import contextlib

import icalendar
import pytest

from icloud_mcp.cal import CalendarService
from icloud_mcp.config import Settings

OWN = "me@icloud.com"


def event(organizer: str) -> str:
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN",
        "BEGIN:VEVENT",
        "UID:seq@test",
        "DTSTAMP:20300101T080000Z",
        "DTSTART:20300301T100000Z",
        "DTEND:20300301T110000Z",
        "SUMMARY:Planning",
        f"ORGANIZER:mailto:{organizer}",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:guest@example.org",
        "SEQUENCE:3",
        "END:VEVENT", "END:VCALENDAR", ""])


@pytest.fixture
def make(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME=OWN, ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16,
                     DEFAULT_TIMEZONE="Europe/Berlin", ALLOW_CALENDAR_INVITES="true").items():
        monkeypatch.setenv(k, v)

    def _make(organizer: str):
        obj = type("Obj", (), {"data": event(organizer), "save": lambda self, **kw: None})()
        fake_cal = type("Cal", (), {"url": "https://caldav.example/home/", "name": "Home"})()
        monkeypatch.setattr(CalendarService, "_principal", lambda self: contextlib.nullcontext(object()))
        monkeypatch.setattr(CalendarService, "_find", lambda self, p, uid, calendar: (fake_cal, obj))
        return CalendarService(Settings.from_env()), obj
    return _make


def seq(obj) -> int:
    return int(next(iter(icalendar.Calendar.from_ical(obj.data).walk("VEVENT")))["sequence"])


def test_private_edits_keep_the_sequence(make):
    svc, obj = make(OWN)
    svc.update_event("seq@test", alarms_minutes_before=[30])
    svc.update_event("seq@test", summary="Planning (notes)", description="agenda")
    assert seq(obj) == 3


def test_a_new_time_bumps_the_sequence(make):
    svc, obj = make(OWN)
    svc.update_event("seq@test", start="2030-03-01T12:00")
    assert seq(obj) == 4


def test_an_attendee_never_bumps_the_organizers_sequence(make):
    svc, obj = make("boss@example.org")
    svc.update_event("seq@test", start="2030-03-01T12:00")
    assert seq(obj) == 3
