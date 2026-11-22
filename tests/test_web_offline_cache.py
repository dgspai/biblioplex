"""Service worker: network first, last good copy when the server fails or is unreachable."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SW = ROOT / "web" / "sw.js"

PRELUDE = r"""
const store = new Map();
const cache = {
  match: async (r) => store.get(typeof r === "string" ? r : r.url)?.clone(),
  put: async (r, res) => { const k = typeof r === "string" ? r : r.url; store.delete(k); store.set(k, res); },
  keys: async () => [...store.keys()].map((u) => new Request(u)),
  delete: async (r) => store.delete(r.url),
};
globalThis.caches = { open: async () => cache };
let handler;
globalThis.self = {
  location: new URL("http://h/"),
  addEventListener: (t, f) => { if (t === "fetch") handler = f; },
  skipWaiting: () => {}, clients: { claim: async () => {} },
};
let net;
globalThis.fetch = async (req) => net(req);
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
async function go(url, init = {}) {
  const waits = [];
  const e = { request: new Request(url, init), respondWith(p) { this.p = p; }, waitUntil: (p) => waits.push(p) };
  handler(e);
  if (!e.p) return "passthrough";
  const res = await e.p; await Promise.all(waits);
  return res.status + " " + (await res.text());
}
"""


def _run(tmp_path: Path, code: str) -> list[str]:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    script = tmp_path / "sw-test.mjs"
    script.write_text(PRELUDE + SW.read_text() + "\n" + code)
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()


def test_serves_last_good_copy_when_server_fails_or_is_gone(tmp_path: Path) -> None:
    out = _run(tmp_path, """
      const u = "http://h/api/cards/search?q=bolt";
      net = async () => json({ n: 1 }); console.log(await go(u));
      net = async () => json({ n: 2 }); console.log(await go(u));
      net = async () => json({ detail: "timeout" }, 500); console.log(await go(u));
      net = async () => { throw new TypeError("offline"); }; console.log(await go(u));
    """)
    assert out == ['200 {"n":1}', '200 {"n":2}', '200 {"n":2}', '200 {"n":2}']


def test_uncached_failure_and_client_errors_pass_through(tmp_path: Path) -> None:
    out = _run(tmp_path, """
      net = async () => json({ detail: "x" }, 500); console.log(await go("http://h/api/a"));
      net = async () => json({ n: 1 }); await go("http://h/api/b");
      net = async () => json({ detail: "bad q" }, 422); console.log(await go("http://h/api/b"));
    """)
    assert out == ['500 {"detail":"x"}', '422 {"detail":"bad q"}']


def test_writes_and_other_origins_are_untouched(tmp_path: Path) -> None:
    out = _run(tmp_path, """
      net = async () => json({});
      console.log(await go("http://h/api/inventory", { method: "POST", body: "{}" }));
      console.log(await go("https://cards.scryfall.io/small/x.jpg"));
    """)
    assert out == ["passthrough", "passthrough"]


def test_any_page_url_opens_offline_from_the_cached_shell(tmp_path: Path) -> None:
    out = _run(tmp_path, """
      const nav = (u) => { const r = new Request(u); Object.defineProperty(r, "mode", { value: "navigate" }); return r; };
      net = async () => new Response("<html>", { headers: { "content-type": "text/html" } });
      const e1 = { request: nav("http://h/?tab=decks"), respondWith(p) { this.p = p; }, waitUntil: () => {} };
      handler(e1); await e1.p; await new Promise((r) => setTimeout(r, 10));
      net = async () => { throw new TypeError("offline"); };
      const e2 = { request: nav("http://h/?tab=combos&combo=1"), respondWith(p) { this.p = p; }, waitUntil: () => {} };
      handler(e2); console.log(await (await e2.p).text());
    """)
    assert out == ["<html>"]


def test_cache_stays_bounded(tmp_path: Path) -> None:
    out = _run(tmp_path, """
      net = async () => json({});
      for (let i = 0; i < MAX_ENTRIES + 20; i++) await go("http://h/api/x?i=" + i);
      console.log(store.size, store.has("http://h/api/x?i=0"), store.has("http://h/api/x?i=" + (MAX_ENTRIES + 19)));
    """)
    assert out == [f"{_max()} false true"]


def _max() -> int:
    return int(SW.read_text().split("MAX_ENTRIES = ")[1].split(";")[0])


def test_registered_and_shipped() -> None:
    assert 'serviceWorker.register("sw.js")' in (ROOT / "web" / "js" / "00-core.js").read_text()
    assert "sw.js" in (ROOT / "web" / "Dockerfile").read_text()
    assert '"sw.js"' in (ROOT / "scripts" / "check_served.sh").read_text()
