"""The accounts file, version 2 (accounts.py): several accounts per area with one default each, read from the first layout in memory.

tests/fixtures/accounts_v1_golden.json holds accounts.json files in the first layout and what the code before this change made of
them (env_overlay, Settings.from_env, the dashboard's list). Those numbers came from that code, not from the current one, so a
change in how a saved setup is read shows up here as a difference. One change is on purpose: since 2026-11 there are only the
providers iCloud and "Other email & calendar", so the list names a WEB.DE or Gmail account "other" (signed in with a password) and
the From line of a waiting mail names no provider for it. What the server runs with (env_overlay, settings) is unchanged.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

import pytest

from icloud_mcp import accounts as A
from icloud_mcp.config import Settings
from icloud_mcp.dashboard_accounts import listing, mail_account

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "accounts_v1_golden.json").read_text())
IDS_MADE_UP = {"accounts_without_ids", "two_accounts_share_one_id"}      # a file without usable ids gets fresh ones when it is read


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="deploy@example.org", ICLOUD_APP_PASSWORD="dddd-eeee-ffff-gggg", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    for k in ("READ_ONLY", "ALLOW_SEND", "IMAP_HOST", "CALDAV_URL", "ENABLE_MAIL", "READ_ONLY_AREAS", "CALENDARS_VISIBLE"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _write(tmp_path, content) -> None:
    """Put a fixture's file in place: None = no file, {"text": ...} = those bytes as they are, anything else = that JSON."""
    if content is None:
        return
    text = content["text"] if isinstance(content, dict) and set(content) == {"text"} else json.dumps(content)
    (tmp_path / A.ACCOUNTS_FILE).write_text(text)


def _plain(v):
    return list(v) if isinstance(v, tuple) else v


def _case(name):
    return next(c for c in GOLDEN if c["name"] == name)


# ------------------------------------------------------------ the first layout reads exactly as before
@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["name"])
def test_a_first_layout_file_gives_the_environment_it_always_gave(env, case):
    _write(env, case["file"])
    assert A.env_overlay(str(env)) == case["env_overlay"]


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["name"])
def test_a_first_layout_file_gives_the_settings_it_always_gave(env, case):
    _write(env, case["file"])
    s = Settings.from_env()
    assert {f: _plain(getattr(s, f)) for f in case["settings"]} == case["settings"]


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["name"])
def test_a_first_layout_file_gives_the_dashboard_list_it_always_gave(env, case):
    _write(env, case["file"])
    s = Settings.from_env()
    got = [{k: v for k, v in row.items() if k != "default_for"} for row in listing(s)]
    want = case["listing"]
    if case["name"] in IDS_MADE_UP:
        ids = [r["id"] for r in got]
        assert all(ids) and len(set(ids)) == len(ids)
        got, want = [{k: v for k, v in r.items() if k != "id"} for r in got], [{k: v for k, v in r.items() if k != "id"} for r in want]
    assert got == want and mail_account(s) == case["mail_account"]


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["name"])
def test_a_saved_and_read_back_file_still_gives_the_same_environment(env, case):
    """Once the owner saves something on /setup the file is the new layout; what the server runs with must not move."""
    _write(env, case["file"])
    data = A.load(str(env))
    if data is None:
        pytest.skip("not an accounts file: nothing to save")
    A.save(str(env), data)
    assert json.loads((env / A.ACCOUNTS_FILE).read_text())["version"] == 2
    assert A.env_overlay(str(env)) == case["env_overlay"]


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["name"])
def test_what_is_read_always_holds_together(env, case):
    _write(env, case["file"])
    data = A.load(str(env))
    if data is not None:
        assert A.problems(data) == [] and data["version"] == 2


def test_reading_writes_nothing_and_leaves_no_copy(env):
    _write(env, _case("webde_mail_icloud_dav_read_only")["file"])
    path = env / A.ACCOUNTS_FILE
    before, stat = path.read_bytes(), path.stat().st_mtime_ns
    A.load(str(env)), A.env_overlay(str(env)), A.current(Settings.from_env()), listing(Settings.from_env())
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stat and os.listdir(env) == [A.ACCOUNTS_FILE]


# ------------------------------------------------------------ the new layout
def _two(mail_default="m1"):
    return {"version": 2, "calendars_visible": [], "defaults": {"mail": mail_default, "calendar": "m2"},
            "accounts": [{"id": "m1", "provider": "webde", "username": "a@example.net", "password": "pw-one", "areas": ["mail"],
                          "auth": "app_password"},
                         {"id": "m2", "provider": "icloud", "username": "b@example.org", "password": "abcd-efgh-ijkl-mnop",
                          "areas": ["mail", "calendar"], "auth": "app_password"}]}


def test_each_area_comes_from_its_default_account_even_when_two_accounts_hold_it(env):
    _write(env, _two("m1"))
    mail = A.env_overlay(str(env))
    assert mail["IMAP_HOST"] == "imap.web.de" and mail["MAIL_PASSWORD"] == "pw-one" and mail["CALDAV_USERNAME"] == "b@example.org"
    _write(env, _two("m2"))
    other = A.env_overlay(str(env))
    assert other["IMAP_HOST"] == "imap.mail.me.com" and other["MAIL_PASSWORD"] == "abcd-efgh-ijkl-mnop"


def test_the_first_layout_has_no_defaults_and_the_account_listed_last_wins(env):
    data = {"accounts": _two()["accounts"], "calendars_visible": []}      # no "defaults", no "version"
    assert A.by_area(data)["mail"]["id"] == "m2"
    _write(env, data)
    assert A.load(str(env))["defaults"] == {"mail": "m2", "calendar": "m2"}


def test_a_default_that_cannot_stand_gives_way_to_the_account_listed_last(env):
    for bad in ("nobody", "m1-but-not-for-calendar"):
        data = _two()
        data["defaults"] = {"calendar": "m1" if bad.startswith("m1") else bad}
        _write(env, data)
        got = A.load(str(env))
        assert got["defaults"] == {"mail": "m2", "calendar": "m2"} and A.problems(got) == []


def test_areas_keep_the_order_they_first_appear_in(env):
    data = {"accounts": [dict(_two()["accounts"][1], areas=["contacts", "calendar", "mail"], read_only=True)], "calendars_visible": []}
    _write(env, data)
    assert list(A.by_area(A.load(str(env)))) == ["contacts", "calendar", "mail"]
    assert A.env_overlay(str(env))["READ_ONLY_AREAS"] == "contacts,calendar,mail"


def test_save_writes_the_new_layout_with_every_key_older_code_reads(env):
    A.save(str(env), {"accounts": [{"id": "a", "provider": "icloud", "username": "x@example.org", "password": "abcd-efgh-ijkl-mnop",
                                    "areas": ["mail", "contacts"]}], "calendars_visible": ["https://c.example.org/1/"]})
    path = env / A.ACCOUNTS_FILE
    raw = json.loads(path.read_text())
    assert list(raw) == ["version", "accounts", "defaults", "calendars_visible"]
    assert raw["version"] == 2 and raw["defaults"] == {"mail": "a", "contacts": "a"} and raw["accounts"][0]["auth"] == "app_password"
    assert raw["calendars_visible"] == ["https://c.example.org/1/"] and oct(path.stat().st_mode & 0o777) == "0o600"
    assert all(k in raw["accounts"][0] for k in ("id", "provider", "username", "password", "areas"))


def test_a_save_after_an_area_changes_hands_moves_its_default(env):
    """What /setup does when a second account takes over an area: the old default no longer holds it."""
    data = _two()
    data["accounts"][1]["areas"] = ["calendar"]
    A.save(str(env), data)
    assert json.loads((env / A.ACCOUNTS_FILE).read_text())["defaults"] == {"mail": "m1", "calendar": "m2"}
    data = {"accounts": [dict(data["accounts"][0], areas=["mail", "calendar"]), dict(data["accounts"][1], areas=["contacts"])],
            "defaults": data["defaults"], "calendars_visible": []}
    A.save(str(env), data)
    assert json.loads((env / A.ACCOUNTS_FILE).read_text())["defaults"] == {"mail": "m1", "calendar": "m1", "contacts": "m2"}


def test_oauth_is_a_known_kind_with_no_logic_and_an_unknown_one_falls_back(env):
    data = _two()
    data["accounts"][0]["auth"] = "oauth"
    A.save(str(env), data)
    assert A.load(str(env))["accounts"][0]["auth"] == "oauth"
    assert A.env_overlay(str(env))["IMAP_HOST"] == "imap.web.de"                       # signs in like any account for now
    data["accounts"][0]["auth"] = "carrier-pigeon"
    _write(env, data)
    assert A.load(str(env))["accounts"][0]["auth"] == "app_password"


# ------------------------------------------------------------ checks
def _valid():
    return A._complete({"accounts": _two()["accounts"], "defaults": {"mail": "m1", "calendar": "m2"}, "calendars_visible": []})


def test_a_sound_model_has_no_problems():
    assert A.problems(_valid()) == []


@pytest.mark.parametrize("change, note", [
    (lambda d: d.update(version=1), "version"),
    (lambda d: d["accounts"][1].update(id="m1"), "repeats the id"),
    (lambda d: d["accounts"][0].pop("id"), "has no id"),
    (lambda d: d["accounts"][0].update(id=""), "has no id"),
    (lambda d: d["accounts"][0].update(auth="telepathy"), "unknown sign-in kind"),
    (lambda d: d["accounts"][0].update(areas=["mail", "todos"]), "unknown areas"),
    (lambda d: d["defaults"].update(mail="ghost"), "not a known account"),
    (lambda d: d["defaults"].update(calendar="m1"), "without that area"),
    (lambda d: d["defaults"].update(todos="m1"), "unknown area"),
    (lambda d: d["defaults"].pop("calendar"), "calendar has accounts but no default"),
    (lambda d: d.pop("defaults"), "defaults is missing"),
    (lambda d: d.update(accounts="m1"), "not a list of accounts"),
])
def test_a_model_that_does_not_hold_together_is_named(change, note):
    d = _valid()
    change(d)
    assert any(note in p for p in A.problems(d)), A.problems(d)


def test_the_notes_never_carry_an_address_a_password_or_an_id():
    d = _valid()
    d["accounts"][1]["id"] = "m1"
    d["defaults"]["mail"] = "ghost"
    text = " ".join(A.problems(d))
    assert text and not any(x in text for x in ("example", "pw-one", "abcd", "ghost", "m1"))


def test_save_refuses_a_model_that_would_not_read_back_and_writes_nothing(env):
    data = _two()
    data["accounts"][1]["id"] = "m1"
    with pytest.raises(ValueError, match="do not hold together"):
        A.save(str(env), data)
    assert os.listdir(env) == []


# ------------------------------------------------------------ ids
def test_an_id_is_random_and_says_nothing_about_the_account():
    ids = {A.new_id() for _ in range(50)}
    assert len(ids) == 50 and all(re.fullmatch(r"[0-9a-f]{8}", i) for i in ids)


def test_a_new_id_skips_one_that_is_taken(monkeypatch):
    drawn = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(A.secrets, "token_hex", lambda n: next(drawn))
    assert A.new_id({"aaaaaaaa"}) == "bbbbbbbb"


def test_two_accounts_with_one_id_are_told_apart_when_read(env):
    _write(env, _case("two_accounts_share_one_id")["file"])
    data = A.load(str(env))
    assert data["accounts"][0]["id"] == "same" and data["accounts"][1]["id"] not in ("", "same")
    assert data["defaults"]["mail"] == data["accounts"][0]["id"] and data["defaults"]["calendar"] == data["accounts"][1]["id"]


# ------------------------------------------------------------ the dashboard's list
def test_the_list_marks_the_default_account_of_each_area(env):
    _write(env, _two("m1"))
    rows = {r["id"]: r for r in listing(Settings.from_env())}
    assert rows["m1"]["default_for"] == ["Mail"] and rows["m2"]["default_for"] == ["Calendar"]
    assert mail_account(Settings.from_env()) == ""                       # a WEB.DE file reads as another provider
    _write(env, _two("m2"))
    rows = {r["id"]: r for r in listing(Settings.from_env())}
    assert rows["m1"]["default_for"] == [] and rows["m2"]["default_for"] == ["Mail", "Calendar"]


def test_with_one_account_per_area_each_account_is_the_default_of_all_its_areas(env):
    _write(env, _case("webde_mail_icloud_dav_read_only")["file"])
    assert [r["default_for"] for r in listing(Settings.from_env())] == [["Mail"], ["Calendar", "Contacts"]]


def test_the_deployments_own_account_is_the_default_of_what_it_serves(env):
    rows = listing(Settings.from_env())
    assert [(r["id"], r["default_for"]) for r in rows] == [("env", ["Mail", "Calendar", "Contacts"])]


# ------------------------------------------------------------ settings_for_account
def _same(t, s, fields):
    return {f: _plain(getattr(t, f)) for f in fields} == {f: _plain(getattr(s, f)) for f in fields}


MAIL_FIELDS = ("imap_host", "imap_port", "imap_security", "imap_username", "smtp_host", "smtp_port", "smtp_security", "smtp_username",
               "mail_secret", "email_address", "display_name")
CAL_FIELDS = ("caldav_url", "caldav_username", "caldav_secret", "calendars_visible")
CARD_FIELDS = ("carddav_url", "carddav_username", "carddav_secret")


@pytest.mark.parametrize("case", [c for c in GOLDEN if c["file"] is not None and c["env_overlay"] and c["name"] not in IDS_MADE_UP
                                  and c["env_overlay"].get("ICLOUD_USERNAME") != A.PENDING_USERNAME], ids=lambda c: c["name"])
def test_the_account_of_an_area_runs_that_area_like_the_server_does(env, case):
    """settings_for_account(default account of an area) agrees with Settings.from_env on everything that area uses. (Not for files
    whose accounts have no usable id: they get new ones on every read until the owner saves, so there is no id to ask by.)"""
    _write(env, case["file"])
    s = Settings.from_env()
    data = A.load(str(env))
    for area, fields in (("mail", MAIL_FIELDS), ("calendar", CAL_FIELDS), ("contacts", CARD_FIELDS)):
        if area in data["defaults"]:
            assert _same(A.settings_for_account(s, data["defaults"][area]), s, fields), (case["name"], area)
    if len(data["accounts"]) == 1:
        t = A.settings_for_account(s, data["accounts"][0]["id"])
        assert _same(t, s, ("enable_mail", "enable_calendar", "enable_contacts", "read_only_areas", "username", "app_password"))


def test_the_deployments_own_account_can_be_asked_for_by_its_id(env):
    s = Settings.from_env()
    t = A.settings_for_account(s, "env")
    assert _same(t, s, MAIL_FIELDS + CAL_FIELDS + CARD_FIELDS + ("username", "app_password", "enable_mail", "read_only_areas"))


def test_another_account_gets_only_its_own_sign_in_and_servers(env):
    _write(env, _two("m1"))
    s = Settings.from_env()
    other = A.settings_for_account(s, "m2")
    assert (other.imap_host, other.mail_secret, other.imap_username) == ("imap.mail.me.com", "abcd-efgh-ijkl-mnop", "b@example.org")
    assert (other.enable_mail, other.enable_calendar, other.enable_contacts) == (True, True, False)
    assert (s.imap_host, s.mail_secret) == ("imap.web.de", "pw-one")                    # the default for mail is the other account
    assert other.carddav_url == "https://contacts.icloud.com" and other.caldav_secret == "abcd-efgh-ijkl-mnop"


def test_the_pools_and_the_deployments_switches_stay_and_only_the_default_calendar_account_has_the_calendar_filter(env, monkeypatch):
    monkeypatch.setenv("IMAP_POOL_SIZE", "5")
    monkeypatch.setenv("READ_ONLY", "false")
    data = _two()
    data["calendars_visible"] = ["https://p01-caldav.icloud.com/1/calendars/work/"]
    data["accounts"][0]["areas"] = ["mail", "calendar"]
    data["accounts"][0]["read_only"] = True
    _write(env, data)
    s = Settings.from_env()
    assert s.imap_pool_size == 5
    one, two = A.settings_for_account(s, "m1"), A.settings_for_account(s, "m2")
    assert one.imap_pool_size == two.imap_pool_size == 5 and (one.caldav_pool_size, one.caldav_keepalive_seconds) == \
        (s.caldav_pool_size, s.caldav_keepalive_seconds)
    assert two.calendars_visible == ("https://p01-caldav.icloud.com/1/calendars/work",) and one.calendars_visible == ()
    assert one.read_only_areas == ("mail", "calendar") and two.read_only_areas == () and not one.writable("mail") and two.writable("mail")


def test_an_unknown_account_is_refused_without_a_word_about_which(env):
    _write(env, _two())
    s = Settings.from_env()
    for bad in ("nobody", "", None, 7):
        with pytest.raises(A.UnknownAccount) as e:
            A.settings_for_account(s, bad)
        assert "nobody" not in str(e.value) and isinstance(e.value, LookupError)


def test_the_resolver_changes_nothing_for_the_server_or_the_file(env):
    _write(env, _two())
    s = Settings.from_env()
    before = (env / A.ACCOUNTS_FILE).read_bytes()
    A.settings_for_account(s, "m2")
    assert (env / A.ACCOUNTS_FILE).read_bytes() == before and dataclasses.replace(s) == s


def test_nothing_in_the_server_calls_the_resolver_yet():
    src = Path(A.__file__).parent
    users = [p.name for p in src.glob("*.py") if p.name != "accounts.py" and "settings_for_account" in p.read_text()]
    assert users == []
