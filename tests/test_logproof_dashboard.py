"""Package 8 (docs/SENDGATE.md) on the dashboard: Settings › Activity log › Proof, Download log, Start a new log tree, the notice
switch and the health check. Made-up data only."""
import json
import sys
from pathlib import Path

from icloud_mcp.gate import logproof as L

from test_dashboard import client, signed_in, web  # noqa: F401  (web is a fixture)
from test_notices_dashboard import HOSTS, URL, confirm, quiet
from test_settings import api, stale

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
import verify_log as V  # noqa: E402


async def test_proof_download_and_new_tree_need_a_recent_sign_in(web):  # noqa: F811
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        p = (await c.get("/dashboard/api/log/proof")).json()
        assert p["on"] and p["verifier_key"].startswith(p["origin"] + "+") and p["size"] >= 1 and not p["broken"]
        assert "seed" not in json.dumps(p)
        stale(d)
        r = await c.post("/dashboard/api/log/export", headers=api(token))
        assert r.status_code == 403 and r.json()["confirm"] is True
        r = await c.post("/dashboard/api/log/new-tree", headers=api(token))
        assert r.status_code == 409                                             # an intact log is never started over
        token = await confirm(c, token)
        r = await c.post("/dashboard/api/log/export", headers=api(token))
        assert r.status_code == 200
        body = r.json()
        assert body["name"].startswith("icloud-mcp-log-") and body["file"]["verifier_key"] == p["verifier_key"]
        assert V.verify(body["file"], p["verifier_key"])["entries"] == len(body["file"]["entries"])
        acts = (await c.get("/dashboard/api/activity?days=1&action=settings")).json()
        assert any(e["what"].startswith("downloaded the activity log") for day in acts["days"] for e in day["entries"])
        seed = json.loads((Path(d.s.data_dir) / L.KEY_FILE).read_text())["seed"]
        assert seed not in r.text and seed not in json.dumps(acts)


async def test_a_changed_log_shows_and_a_new_tree_starts_after_confirming(web):  # noqa: F811
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    audit = d.audit
    async with client(app) as c:
        token = await signed_in(c)
        path = Path(d.s.data_dir) / "audit.jsonl"
        lines = path.read_bytes().split(b"\n")[:-1]
        e = json.loads(lines[0])
        e["summary"] = (e.get("summary") or "") + " (changed)"
        lines[0] = json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode()
        path.write_bytes(b"".join(x + b"\n" for x in lines))
        with audit._lock:                                                       # the next checkpoint finds the change
            assert audit.proof.sign(audit._lines(), chain_break=lambda: 0) is None
        audit._after()
        p = (await c.get("/dashboard/api/log/proof")).json()
        assert p["broken"] and p["broken"]["at"] == 0
        report = mcp._icloud_health()
        assert report["activity_log"]["ok"] is False and "new log tree" in report["activity_log"]["problem"]
        old = p["origin"]
        stale(d)
        r = await c.post("/dashboard/api/log/new-tree", headers=api(token))
        assert r.status_code == 403 and r.json()["confirm"] is True              # criterion 5: refused with confirm
        token = await confirm(c, token)
        r = await c.post("/dashboard/api/log/new-tree", headers=api(token))
        assert r.status_code == 200 and r.json()["origin"] != old and not r.json()["broken"]
        assert mcp._icloud_health()["activity_log"]["ok"] is True


async def test_the_log_checkpoints_switch_needs_a_recent_sign_in_to_turn_on(web):  # noqa: F811
    app, mcp, _, _ = web(**HOSTS)
    d = mcp._icloud_dashboard
    svc = quiet(mcp)
    rec, _ = svc.add(URL)
    assert d.audit.on_checkpoint == svc.offer_checkpoint                       # the dashboard wired the two together
    async with client(app) as c:
        token = await signed_in(c)
        stale(d)
        r = await c.post(f"/dashboard/api/notices/{rec['id']}/change", headers=api(token), json={"checkpoints": True})
        assert r.status_code == 403 and r.json()["confirm"] is True
        token = await confirm(c, token)
        r = await c.post(f"/dashboard/api/notices/{rec['id']}/change", headers=api(token), json={"checkpoints": True})
        assert r.status_code == 200 and r.json()["receiver"]["checkpoints"] is True
        stale(d)
        r = await c.post(f"/dashboard/api/notices/{rec['id']}/change", headers=api(token), json={"checkpoints": False})
        assert r.status_code == 200 and r.json()["receiver"]["checkpoints"] is False   # turning it off needs nothing


def test_the_health_check_shows_the_verifier_key_and_never_the_key(web):  # noqa: F811
    app, mcp, _, _ = web()
    report = mcp._icloud_health()
    log = report["activity_log"]
    assert log["ok"] is True and "+" in log["verifier_key"] and log["size"] >= 0
    seed = json.loads((Path(mcp._icloud_dashboard.s.data_dir) / L.KEY_FILE).read_text())["seed"]
    assert seed not in json.dumps(report)
