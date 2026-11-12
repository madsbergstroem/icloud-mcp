"""The dashboard's modules load with one version stamp: the page asks for app.js?v=<hash of static/app>, and every module that
app.js imports, directly or through another module, is asked for with the same ?v=. After an update a normal reload fetches the
new modules even where a browser kept the old ones under their plain address."""
import re
from pathlib import Path

from test_dashboard import client, web  # noqa: F401  (web is a fixture)

APP = Path(__file__).resolve().parents[1] / "src" / "icloud_mcp" / "static" / "app"
IMPORT = re.compile(r'(?:\bfrom\s*|\bimport\s*\(?\s*)"(\./[^"]+)"')


async def test_every_module_is_imported_with_the_pages_version(web):
    app, *_ = web()
    async with client(app) as c:
        page = (await c.get("/dashboard")).text
        v = re.search(r'/dashboard/app/app\.js\?v=([0-9a-f]+)"', page).group(1)
        assert f"/dashboard/app/app.css?v={v}" in page
        seen, todo = set(), ["app.js"]
        while todo:                                                    # follow the imports the way a browser does
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            r = await c.get(f"/dashboard/app/{name}?v={v}")
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript"), name
            for spec in IMPORT.findall(r.text):
                assert spec.endswith(f"?v={v}"), (name, spec)          # never a plain "./api.js"
                todo.append(spec[2:].split("?")[0])
    modules = {p.name for p in APP.glob("*.js")} - {"config.js"}      # config.js is a classic script the page loads with ?v= itself
    assert seen == modules


async def test_the_source_files_stay_plain_and_other_files_are_served_as_they_are(web):
    assert all('.js?v=' not in p.read_text() for p in APP.glob("*.js"))   # the stamp is added when served, not written into the source
    app, *_ = web()
    async with client(app) as c:
        css = await c.get("/dashboard/app/app.css")
        assert css.status_code == 200 and css.text == (APP / "app.css").read_text()
        assert (await c.get("/dashboard/app/index.html")).status_code == 404
