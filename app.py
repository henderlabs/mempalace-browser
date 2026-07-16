#!/usr/bin/env python3
"""
MemPalace Browser — a read-only web browser for a private MemPalace.

Runs on the MemPalace tool venv's own interpreter, so it imports the exact same
mempalace package the MCP server uses. There is no second install and no MP_BIN,
which is the failure mode that killed the old FastAPI shim: it shelled out to a
stale binary and reported success while reading nothing.

It also deliberately does NOT read chroma.sqlite3 directly. Chroma's tables are
internal implementation (MemPalace ships `mempalace migrate` precisely because
that schema moves). Going through the package means this cannot drift.

Read-only by construction: every collection is opened with create=False, so a
wrong path fails loudly instead of silently manufacturing an empty palace.

Stdlib only. No pip install, nothing to rot.

Unofficial; not affiliated with the MemPalace project. MIT licensed.
Styling follows the HenderLabs brand palette.
"""

import html
import http.server
import json
import os
import re
import shutil
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config
# Localhost by default: this is a browser for YOUR private palace, and drawers
# routinely hold health, journal and personal material. Binding to all
# interfaces would expose that to every device on your network — including the
# ones you did not think about. Change it deliberately, not by default.
BIND = os.environ.get("MPB_BIND", "127.0.0.1")
PORT = int(os.environ.get("MPB_PORT", "8080"))

# DNS-rebinding defence. "No auth because it's localhost" only holds if the
# browser cannot be tricked into treating us as same-origin. A malicious page
# can point its own domain at 127.0.0.1, at which point the same-origin policy
# protects nothing and it can read every drawer through /api/data. Checking the
# Host header costs nothing and closes it: a rebound request arrives claiming
# Host: evil.example, which is not in this set.
_DEFAULT_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
ALLOWED_HOSTS = set(_DEFAULT_HOSTS)
if BIND not in ("0.0.0.0", "::", ""):
    ALLOWED_HOSTS.add(BIND.lower())
for _h in os.environ.get("MPB_ALLOWED_HOSTS", "").split(","):
    if _h.strip():
        ALLOWED_HOSTS.add(_h.strip().lower())
PYPI_TTL = 3600           # seconds to cache the PyPI lookup
PALACE_TTL = 30           # seconds to cache the palace read
PYPI_URL = "https://pypi.org/pypi/mempalace/json"

# Demo mode renders synthetic drawers and never imports mempalace. It exists so
# you can see the interface without pointing it at your own memory, so the
# README screenshot contains nobody's real life, and so the tests can exercise
# the whole server without installing a vector database in CI.
DEMO = os.environ.get("MPB_DEMO") == "1"

# The only outbound request this program ever makes is the PyPI version check,
# and it is disclosed in the README. It sends no palace data — it fetches a
# fixed URL and reads one version string. Set MPB_CHECK_UPDATES=0 to make the
# program fully offline; the chip then reads "update check off", which is
# honest, rather than pretending to know.
#
# Demo mode defaults it OFF: a demonstration should not quietly reach the
# network, and comparing a fake version against real PyPI would render "update
# available" for a version that does not exist.
CHECK_UPDATES = os.environ.get("MPB_CHECK_UPDATES", "0" if DEMO else "1") != "0"

if DEMO:
    MP_VERSION = "0.0.0-demo"
    PALACE_PATH = "(demo — no palace is being read)"
    COLLECTION = "demo"
    BACKEND = "demo"
    get_collection = None
else:
    try:
        from mempalace.version import __version__ as MP_VERSION
        from mempalace.palace import get_collection
        from mempalace.config import MempalaceConfig
    except ImportError:
        sys.exit(
            "ERROR: cannot import mempalace.\n"
            "Run this with an interpreter that has MemPalace installed — use\n"
            "run.sh, which finds it from your `mempalace` command automatically.\n"
            "\n"
            "To see the interface without a palace:  MPB_DEMO=1 ./run.sh"
        )

    # Ask MemPalace where its palace is; never re-derive it. MempalaceConfig
    # resolves MEMPALACE_PALACE_PATH / MEMPAL_PALACE_PATH, then config.json's
    # palace_path key, then the default. Reimplementing that would silently
    # ignore the config of anyone who moved their palace.
    _cfg = MempalaceConfig()
    PALACE_PATH = _cfg.palace_path
    COLLECTION = _cfg.collection_name
    BACKEND = (_cfg.backend or "chroma").lower()

# The palace dir normally lives inside the data dir (~/.mempalace/palace), whose
# parent also holds knowledge_graph.sqlite3, wal/, hallways.json — real
# footprint a storage panel should count.
DATA_PATH = "" if DEMO else (os.path.dirname(PALACE_PATH.rstrip("/")) or PALACE_PATH)

# Only chroma and sqlite_exact keep drawers on this filesystem. For pgvector and
# qdrant the data lives on another host entirely, so local disk figures would be
# a confident lie rather than an error.
LOCAL_BACKENDS = ("chroma", "sqlite_exact")

# ---------------------------------------------------------------- version check
_pypi_cache = {"at": 0.0, "latest": None, "error": None}
_pypi_lock = threading.Lock()


def _parse_version(v):
    parts = []
    for chunk in re.split(r"[.\-+]", v or ""):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts)


def version_info(force=False):
    """Installed vs latest. Never claims 'current' when the check failed."""
    if not CHECK_UPDATES:
        # Say what is true: we did not look. Not "up to date".
        return {
            "installed": MP_VERSION,
            "latest": None,
            "status": "disabled",
            "error": None,
            "checked_at": None,
        }
    with _pypi_lock:
        stale = (time.time() - _pypi_cache["at"]) > PYPI_TTL
        if force or stale or (_pypi_cache["latest"] is None and _pypi_cache["error"] is None):
            try:
                req = urllib.request.Request(
                    PYPI_URL, headers={"User-Agent": "mempalace-browser"}
                )
                with urllib.request.urlopen(req, timeout=6) as r:
                    data = json.load(r)
                _pypi_cache.update(at=time.time(), latest=data["info"]["version"], error=None)
            except Exception as e:
                _pypi_cache.update(at=time.time(), latest=None, error=str(e)[:120])
        latest, error, checked = _pypi_cache["latest"], _pypi_cache["error"], _pypi_cache["at"]

    if latest is None:
        status = "unknown"          # An unreachable PyPI is not "up to date".
    elif _parse_version(latest) > _parse_version(MP_VERSION):
        status = "update-available"
    elif _parse_version(latest) < _parse_version(MP_VERSION):
        status = "ahead"
    else:
        status = "current"

    return {
        "installed": MP_VERSION,
        "latest": latest,
        "status": status,
        "error": error,
        "checked_at": datetime.fromtimestamp(checked, timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- storage
def _dir_size(path):
    total, files = 0, 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
                files += 1
            except OSError:
                pass
    return total, files


def _mount_of(path):
    """Walk up until the device id changes — that's the mount point."""
    path = os.path.realpath(path)
    dev = os.stat(path).st_dev
    while path != "/":
        parent = os.path.dirname(path)
        try:
            if os.stat(parent).st_dev != dev:
                return path
        except OSError:
            break
        path = parent
    return "/"


def storage_info():
    """Local disk figures, or an honest explanation of why there are none."""
    if DEMO:
        # Synthetic, like the drawers. The whole page is labelled DEMO; a
        # storage panel that only ever reads "n/a" cannot show what it is for.
        return {"available": True, "backend": "demo",
                "palace_bytes": 3_842_048, "palace_files": 13,
                "data_bytes": 3_951_616, "data_files": 19,
                "disk_total": 52_521_566_208, "disk_used": 4_202_725_376,
                "disk_free": 48_318_840_832,
                "mount": "/opt/mempalace", "dedicated": True,
                "data_path": "(demo)"}
    if BACKEND not in LOCAL_BACKENDS:
        return {
            "available": False,
            "backend": BACKEND,
            "reason": f"{BACKEND} backend — drawers are stored remotely, not on this disk",
        }
    if not os.path.isdir(PALACE_PATH):
        return {
            "available": False,
            "backend": BACKEND,
            "reason": f"palace path is not a local directory: {PALACE_PATH}",
        }
    try:
        palace_bytes, palace_files = _dir_size(PALACE_PATH)
        data_bytes, data_files = _dir_size(DATA_PATH)
        du = shutil.disk_usage(DATA_PATH)
        mount = _mount_of(DATA_PATH)
    except OSError as e:
        return {"available": False, "backend": BACKEND, "reason": f"cannot stat palace: {e}"}

    # If the palace sits on its own volume, its mount is not "/". That's the
    # difference between "my memory has 47GB of room" and "my memory is
    # competing with the OS for the last 6GB".
    return {
        "available": True,
        "backend": BACKEND,
        "palace_bytes": palace_bytes,
        "palace_files": palace_files,
        "data_bytes": data_bytes,
        "data_files": data_files,
        "disk_total": du.total,
        "disk_used": du.used,
        "disk_free": du.free,
        "mount": mount,
        "dedicated": mount != "/",
        "data_path": DATA_PATH,
    }


# ---------------------------------------------------------------- palace read
_palace_cache = {"at": 0.0, "data": None}
_palace_lock = threading.Lock()


def _demo_drawers():
    """Obviously-synthetic drawers. Nothing here is anyone's real memory."""
    now = datetime.now(timezone.utc)
    spec = [
        ("Orchard", "decisions", 1, "sample-decisions.md",
         "# Chose SQLite over Postgres for the ledger\n\nThe ledger is single-writer and "
         "under a gigabyte. Postgres would add an operational dependency to a program "
         "that otherwise has none. Revisit if concurrent writers ever appear.\n\n"
         "Decided after the March load test showed 40x headroom."),
        ("Orchard", "decisions", 96, "sample-decisions.md",
         "# Rejected the plugin architecture\n\nTwo plugins existed and both were written "
         "by us. The abstraction cost more than it saved. Deleted 900 lines; behaviour "
         "unchanged."),
        ("Orchard", "deploy", 4, "sample-deploy.md",
         "# Deploy is a git archive of the SHA\n\nThe image tag IS the commit SHA, so a "
         "running container always names the source it came from. No `latest`, ever."),
        ("Orchard", "deploy", 38, "sample-deploy.md",
         "# Postmortem: the health check that could not fail\n\nThe status endpoint "
         "returned 200 with the error text in the body, so monitoring saw a healthy "
         "service for six weeks. A health check that cannot fail is not a health check."),
        ("Greenhouse", "notes", 12, "sample-notes.md",
         "# Watering schedule\n\nTomatoes every other day, deeply, in the morning. "
         "Basil daily. The rosemary wants to be forgotten about."),
        ("Greenhouse", "notes", 158, "sample-notes.md",
         "# Seed order arrived\n\nHeirloom tomato, two varieties of basil, and the "
         "pepper seeds that never germinate but hope springs eternal."),
        ("Workshop", "reference", 67, "sample-reference.md",
         "# Torque values\n\nAluminium: go slow, and stop the moment it stops feeling "
         "like it is tightening. The thread will not warn you twice."),
        ("Workshop", "reference", 205, "sample-reference.md",
         "# Sharpening angles\n\nKitchen knives 15 degrees per side. Chisels 25. "
         "The angle matters less than being consistent about it."),
        ("Workshop", "projects", 250, "sample-projects.md",
         "# Bench rebuild\n\nThe top is cupped about 3mm across the width. Flatten with "
         "winding sticks before adding the vice, not after."),
    ]
    out = []
    for wing, room, days_ago, src, content in spec:
        filed = (now - timedelta(days=days_ago)).isoformat(timespec="microseconds")
        out.append({
            "id": f"drawer_{wing}_{room}_{abs(hash((wing, room, content))) % (16**24):024x}",
            "wing": wing, "room": room, "filed_at": filed,
            "added_by": "demo", "source_file": src,
            "content": content, "bytes": len(content.encode("utf-8")),
            "meta": {"wing": wing, "room": room, "filed_at": filed,
                     "added_by": "demo", "source_file": src, "chunk_index": "0"},
        })
    return out


def read_palace(force=False):
    with _palace_lock:
        if not force and _palace_cache["data"] and (time.time() - _palace_cache["at"]) < PALACE_TTL:
            return _palace_cache["data"]

        if DEMO:
            drawers = _demo_drawers()
            drawers.sort(key=lambda d: d["filed_at"], reverse=True)
            now = time.time()
            data = {
                "drawers": drawers, "count": len(drawers),
                "palace_path": PALACE_PATH,
                "read_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                "storage": storage_info(),
                "demo": True,
            }
            _palace_cache.update(at=now, data=data)
            return data

        col = get_collection(
            PALACE_PATH,
            collection_name=COLLECTION,
            create=False,            # never manufacture a palace
        )
        res = col.get(include=["metadatas", "documents"])
        ids = list(res.ids or [])
        metas = list(res.metadatas or [])
        docs = list(res.documents or [])

        drawers = []
        for i, did in enumerate(ids):
            m = dict(metas[i] or {})
            drawers.append({
                "id": did,
                "wing": m.get("wing") or "(unfiled)",
                "room": m.get("room") or "(none)",
                "filed_at": m.get("filed_at") or "",
                "added_by": m.get("added_by") or "",
                "source_file": m.get("source_file") or "",
                "content": docs[i] or "",
                "bytes": len((docs[i] or "").encode("utf-8")),
                "meta": {k: str(v) for k, v in m.items()},
            })

        drawers.sort(key=lambda d: d["filed_at"], reverse=True)
        now = time.time()
        data = {
            "drawers": drawers,
            "count": len(drawers),
            "palace_path": PALACE_PATH,
            "read_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "storage": storage_info(),
        }
        _palace_cache.update(at=now, data=data)
        return data


# ---------------------------------------------------------------- http
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s  %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self):
        """Reject requests whose Host we do not recognise (DNS rebinding)."""
        host = (self.headers.get("Host") or "").strip().lower()
        # Strip the port, being careful with bracketed IPv6 literals.
        if host.startswith("["):
            name = host.partition("]")[0].lstrip("[")
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in ALLOWED_HOSTS

    def _refuse_host(self, path):
        """Explain the refusal in the medium the caller is actually using."""
        host = self.headers.get("Host", "(none)")
        name = host.rsplit(":", 1)[0] if ":" in host and not host.startswith("[") else host
        # Route through log_message rather than sys.stderr directly, so there is
        # one logging path and callers can silence it.
        self.log_message("refused Host %r — not allowed. If this is you: "
                         "MPB_ALLOWED_HOSTS=%s ./run.sh", host, name)
        if path.startswith("/api/"):
            self._send(403, json.dumps({
                "error": "host not allowed",
                "host": host,
                "hint": f"restart with MPB_ALLOWED_HOSTS={name} if this is you",
            }), "application/json")
            return
        # A person in a browser gets a page, not a JSON blob. Without this they
        # see {"error":"host not allowed"} and reasonably conclude it is broken.
        # The Host header is attacker-controlled, so escape it — reflecting it
        # raw would be XSS in the very code path added to stop an attack.
        # Substitute in ONE pass. Chaining .replace() calls would let a value
        # inserted by an earlier pass be re-substituted by a later one — send
        # `Host: {{NAME}}` and the second replace chews on text the first one
        # wrote. Escaping means that is cosmetic rather than exploitable, but a
        # template that reprocesses its own output is a bad habit to ship.
        subs = {"{{HOST}}": html.escape(host), "{{NAME}}": html.escape(name)}
        page = re.sub(r"\{\{(?:HOST|NAME)\}\}",
                      lambda m: subs[m.group(0)], HOST_REFUSED_HTML)
        # Deliberately does NOT print the allowed-host list. Not exploitable —
        # a rebinding attacker cannot forge Host, browsers set it — but it hands
        # internal hostnames to any caller, and the actionable line is the
        # command with their own name in it. The operator sees the full list in
        # the terminal, where it belongs.
        self._send(403, page, "text/html; charset=utf-8")

    def do_GET(self):
        path, _, qs = self.path.partition("?")

        # Host check first: nothing else should happen for a request we do not
        # trust the origin of.
        if not self._host_allowed():
            self._refuse_host(path)
            return

        # Parse properly: `?x=notrefresh=1` must NOT force a re-read.
        force = "1" in urllib.parse.parse_qs(qs).get("refresh", [])
        try:
            if path == "/":
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif path == "/api/data":
                payload = read_palace(force=force)
                payload["version"] = version_info()
                self._send(200, json.dumps(payload), "application/json")
            elif path == "/api/version":
                self._send(200, json.dumps(version_info(force=True)), "application/json")
            elif path == "/api/health":
                # Reports the truth, including failure. Never a blanket "ok".
                try:
                    d = read_palace()
                    self._send(200, json.dumps(
                        {"ok": True, "drawers": d["count"], "palace": PALACE_PATH}
                    ), "application/json")
                except Exception as e:
                    self._send(503, json.dumps({"ok": False, "error": str(e)}), "application/json")
            else:
                self._send(404, json.dumps({"error": "not found"}), "application/json")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(500, json.dumps({"error": str(e)}), "application/json")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


HOST_REFUSED_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MemPalace Browser — host not allowed</title>
<style>
  :root { --brand:#0D6BFF; --bg:#050814; --surface:#080E1E; --fg:#FFFFFF;
          --muted:#BFC7D1; --subtle:#6E7886; --warn:#F59E0B;
          --line:rgb(191 199 209 / .18);
          --mono:ui-monospace,SFMono-Regular,Menlo,monospace; }
  html { color-scheme:dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px;
         background:var(--bg); color:var(--fg);
         font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }
  .card { max-width:620px; background:var(--surface); border:1px solid var(--line);
          border-radius:10px; padding:26px 28px; }
  .wm { font-size:1.125rem; font-weight:700; letter-spacing:-.02em; margin-bottom:18px; }
  .wm span { color:var(--brand); }
  h1 { font-size:15px; margin:0 0 12px; color:var(--warn); }
  p { color:var(--muted); margin:0 0 12px; }
  code, pre { font-family:var(--mono); }
  code { background:#0D1526; padding:1px 5px; border-radius:3px; font-size:12.5px; }
  pre { background:#0D1526; border:1px solid var(--line); border-radius:7px;
        padding:11px 13px; overflow-x:auto; font-size:12.5px; margin:0 0 12px;
        color:var(--fg); }
  .why { border-top:1px solid var(--line); margin-top:18px; padding-top:14px;
         font-size:12.5px; color:var(--subtle); }
</style></head>
<body><div class="card">
  <div class="wm">MemPalace<span> Browser</span></div>
  <h1>Host not allowed: {{HOST}}</h1>
  <p>This is almost certainly a configuration step, not a bug. The browser only
     answers requests whose <code>Host</code> it recognises, and this one is not
     on the list. The full list is printed in the terminal where you started it.</p>
  <p><strong>If this is you</strong>, restart it with your hostname allowed:</p>
  <pre>MPB_ALLOWED_HOSTS={{NAME}} ./run.sh</pre>
  <p>Or reach it over an SSH tunnel, which needs no configuration because your
     browser still says <code>localhost</code>:</p>
  <pre>ssh -L 8080:127.0.0.1:8080 you@this-host</pre>
  <div class="why"><strong>Why this check exists.</strong> There is no password
     here — on localhost, the operating system is the authentication. That only
     holds if a browser cannot be tricked into treating this server as
     same-origin. A malicious page can point its own domain at
     <code>127.0.0.1</code> (DNS rebinding) and would otherwise read every
     drawer in your palace. Checking the <code>Host</code> closes that.</div>
</div></body></html>
"""

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MemPalace Browser</title>
<style>
  /* HenderLabs brand palette. Dark-only by design — no light mode. */
  :root {
    --brand:#0D6BFF; --brand-hover:#3385FF; --graphite:#6E7886;
    --bg:#050814; --bg-alt:#02060D; --surface:#080E1E; --surface-raised:#0D1526;
    --fg:#FFFFFF; --fg-muted:#BFC7D1; --fg-subtle:#6E7886; --fg-disabled:#4A5568;
    --line:rgb(191 199 209 / .18); --line-2:rgb(191 199 209 / .32);
    --success:#22C55E; --warning:#F59E0B; --error:#EF4444;
    --shadow-brand:0 0 0 3px rgb(13 107 255 / .35);
    --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  * { box-sizing:border-box; }
  html { color-scheme:dark; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
         -webkit-font-smoothing:antialiased; }

  header { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
           padding:0 18px; height:56px; background:rgb(5 8 20 / .95);
           border-bottom:1px solid var(--line); backdrop-filter:blur(6px); }
  .wordmark { font-size:1.125rem; font-weight:700; letter-spacing:-.02em;
              margin-right:2px; white-space:nowrap; }
  .wordmark .b { color:var(--brand); }
  .path { font-size:11px; color:var(--fg-subtle); font-family:var(--mono); }

  .chip { font-size:11.5px; padding:3px 9px; border-radius:999px; white-space:nowrap;
          border:1px solid var(--line-2); background:var(--surface); color:var(--fg-muted); }
  .chip b { color:var(--fg); font-weight:600; }
  .chip.act { cursor:pointer; }
  .chip.act:hover { border-color:var(--brand); }
  .chip.current { border-color:rgb(34 197 94 / .5); color:var(--success); }
  .chip.update  { border-color:rgb(245 158 11 / .55); color:var(--warning); }
  .chip.unknown { border-color:rgb(239 68 68 / .5); color:var(--error); }
  .chip.new     { border-color:var(--brand); color:var(--brand); }

  .spacer { flex:1; }
  button.refresh { display:flex; align-items:center; gap:7px; cursor:pointer;
      background:var(--surface); color:var(--fg-muted); border:1px solid var(--line-2);
      border-radius:7px; padding:5px 10px; font:inherit; font-size:12px; }
  button.refresh:hover { border-color:var(--brand); color:var(--fg); }
  button.refresh:focus-visible { outline:none; box-shadow:var(--shadow-brand); }
  button.refresh:disabled { opacity:.5; cursor:default; }
  .refresh .ico { font-size:13px; line-height:1; }
  .refresh.spin .ico { animation:spin .7s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .refresh .ago { color:var(--fg-subtle); font-variant-numeric:tabular-nums; }

  .clock { text-align:right; font-variant-numeric:tabular-nums; line-height:1.25; }
  .clock .local { font-size:13px; font-weight:600; }
  .clock .utc { font-size:10.5px; color:var(--fg-subtle); font-family:var(--mono); }

  main { display:grid; grid-template-columns:272px 1fr; height:calc(100vh - 56px); }
  #side { border-right:1px solid var(--line); background:var(--bg-alt);
          display:grid; grid-template-rows:auto 1fr auto; min-height:0; }
  #sideTop { padding:11px; border-bottom:1px solid var(--line); }
  #tree { overflow-y:auto; padding:8px 11px; min-height:0; }
  #stats { border-top:1px solid var(--line); padding:11px; background:var(--bg-alt); }
  @media (max-width:820px){
    main { grid-template-columns:1fr; height:auto; }
    #side { grid-template-rows:none; } #tree { max-height:40vh; }
  }

  #q { width:100%; padding:8px 10px; border-radius:7px; border:1px solid var(--line-2);
       background:var(--bg); color:var(--fg); font-size:13px; }
  #q::placeholder { color:var(--fg-disabled); }
  #q:focus { outline:none; border-color:var(--brand); box-shadow:var(--shadow-brand); }

  .wing { margin-bottom:1px; }
  .wing > .row { display:flex; align-items:center; gap:7px; padding:5px 7px;
                 border-radius:6px; cursor:pointer; }
  .wing > .row:hover { background:var(--surface); }
  .wing.sel > .row { background:var(--surface-raised); }
  .wing .name { flex:1; font-weight:600; font-size:12.5px; }
  .n { font-size:10.5px; color:var(--fg-subtle); font-variant-numeric:tabular-nums; }
  .dot { width:5px; height:5px; border-radius:50%; background:var(--brand); flex:none; }
  .arrow { color:var(--fg-disabled); font-size:9px; width:8px; transition:transform .12s; }
  .wing.open .arrow { transform:rotate(90deg); }
  .rooms { display:none; margin:1px 0 4px 14px; border-left:1px solid var(--line); }
  .wing.open .rooms { display:block; }
  .room { display:flex; align-items:center; gap:6px; padding:4px 8px; cursor:pointer;
          border-radius:0 6px 6px 0; }
  .room:hover { background:var(--surface); }
  .room.sel { background:rgb(13 107 255 / .16); color:var(--brand); }
  .room .name { flex:1; font-size:12px; }

  /* Stats — deliberately restrained. The brand guide says complexity only when
     the problem demands it; this is four numbers and twelve bars. */
  .st-h { font-size:10px; text-transform:uppercase; letter-spacing:.07em;
          color:var(--fg-subtle); margin-bottom:7px; font-weight:600; }
  .st-row { display:flex; justify-content:space-between; font-size:11.5px;
            margin-bottom:3px; }
  .st-row span:first-child { color:var(--fg-subtle); }
  .st-row span:last-child { font-variant-numeric:tabular-nums; font-family:var(--mono); }
  .bar { height:4px; border-radius:2px; background:var(--surface-raised);
         overflow:hidden; margin:5px 0 3px; }
  .bar > i { display:block; height:100%; background:var(--brand); }
  .bar.warn > i { background:var(--warning); }
  .bar.bad > i  { background:var(--error); }
  .st-note { font-size:10px; color:var(--fg-disabled); margin-top:2px; }
  .spark { display:flex; align-items:flex-end; gap:2px; height:30px; margin-top:6px; }
  .spark > i { flex:1; background:var(--surface-raised); border-radius:1px 1px 0 0;
               min-height:1px; position:relative; }
  .spark > i.has { background:var(--brand); opacity:.55; }
  .spark > i:hover { opacity:1; }
  .spark-x { display:flex; justify-content:space-between; font-size:9px;
             color:var(--fg-disabled); margin-top:3px; }

  #content { overflow-y:auto; padding:16px 20px; }
  .crumb { color:var(--fg-subtle); font-size:12px; margin-bottom:12px; }
  .crumb a { color:var(--brand); cursor:pointer; text-decoration:none; }
  .crumb a:hover { color:var(--brand-hover); }
  .card { background:var(--surface); border:1px solid var(--line); border-radius:8px;
          padding:11px 13px; margin-bottom:8px; cursor:pointer;
          transition:border-color .12s; }
  .card:hover { border-color:var(--brand); }
  .card.isnew { border-left:2px solid var(--brand); }
  .card .top { display:flex; gap:8px; align-items:center; margin-bottom:4px; }
  .card .loc { font-size:11px; color:var(--brand); font-family:var(--mono); }
  .card .when { margin-left:auto; font-size:11px; color:var(--fg-subtle); }
  .pill { font-size:9px; font-weight:700; letter-spacing:.06em; padding:1px 5px;
          border-radius:3px; background:var(--brand); color:#fff; text-transform:uppercase; }
  .card .prev { color:var(--fg-muted); font-size:12.5px; overflow:hidden;
                display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
  mark { background:rgb(245 158 11 / .32); color:var(--fg); border-radius:2px; padding:0 1px; }

  .detail h2 { font-size:14px; margin:0 0 4px; font-family:var(--mono); font-weight:600;
               word-break:break-all; }
  .metagrid { display:grid; grid-template-columns:auto 1fr; gap:3px 12px; margin:12px 0;
              font-size:12px; background:var(--surface); border:1px solid var(--line);
              border-radius:8px; padding:11px 13px; }
  .metagrid dt { color:var(--fg-subtle); font-family:var(--mono); }
  .metagrid dd { margin:0; word-break:break-word; color:var(--fg-muted); }
  pre.doc { background:var(--surface); border:1px solid var(--line); border-radius:8px;
            padding:14px; white-space:pre-wrap; word-wrap:break-word; font-size:12.5px;
            font-family:var(--mono); line-height:1.65; margin:0; color:var(--fg-muted); }
  .empty { color:var(--fg-subtle); text-align:center; padding:50px 20px; }
  .err { color:var(--error); font-family:var(--mono); font-size:12px; }
</style>
</head>
<body>
<header>
  <div class="wordmark">MemPalace<span class="b"> Browser</span></div>
  <span class="path" id="palacePath"></span>
  <span class="chip act" id="verChip" title="Click to re-check PyPI">version…</span>
  <span class="chip" id="cntChip">…</span>
  <span class="chip new" id="newChip" style="display:none"></span>
  <span class="spacer"></span>
  <button class="refresh" id="btnRefresh" title="Re-read the palace">
    <span class="ico">⟳</span><span>Refresh</span><span class="ago" id="ago"></span>
  </button>
  <div class="clock">
    <div class="local" id="clkLocal">—</div>
    <div class="utc" id="clkUtc">—</div>
  </div>
</header>
<main>
  <div id="side">
    <div id="sideTop"><input id="q" placeholder="Search drawers…" autocomplete="off"></div>
    <div id="tree"></div>
    <div id="stats"></div>
  </div>
  <div id="content"><div class="empty">Loading palace…</div></div>
</main>
<script>
let DATA = null, sel = {wing:null, room:null}, query = "", lastSeen = null, readAt = null;

const LS_KEY = "mpb.lastSeen";

// ---- clock: rendered in YOUR browser's timezone. The box runs UTC, so
// server-side rendering would show the wrong time. This is always right.
function tick(){
  const now = new Date();
  document.getElementById("clkLocal").textContent =
    now.toLocaleString(undefined, {weekday:"short", month:"short", day:"numeric",
                                   hour:"2-digit", minute:"2-digit", second:"2-digit"});
  document.getElementById("clkUtc").textContent =
    Intl.DateTimeFormat().resolvedOptions().timeZone + " · "
    + now.toISOString().slice(0,19).replace("T"," ") + "Z";
  if(readAt) document.getElementById("ago").textContent = "· " + ago(readAt);
}
setInterval(tick, 1000); tick();

function ago(iso){
  const s = Math.max(0, (Date.now() - new Date(iso).getTime())/1000);
  if(s < 60) return "just now";
  if(s < 3600) return Math.floor(s/60) + "m ago";
  if(s < 86400) return Math.floor(s/3600) + "h ago";
  return Math.floor(s/86400) + "d ago";
}
const esc = s => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function hl(s, q){
  s = esc(s);
  if(!q) return s;
  try { return s.replace(new RegExp("("+q.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","gi"), "<mark>$1</mark>"); }
  catch(e){ return s; }
}
const when = iso => { if(!iso) return ""; const d=new Date(iso);
  return isNaN(d) ? iso.slice(0,10) : d.toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"}); };
function bytes(n){
  if(n === null || n === undefined) return "—";
  const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while(n >= 1024 && i < u.length-1){ n /= 1024; i++; }
  return (n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)) + " " + u[i];
}

// A drawer is "new" if it was filed after the last time you hit Refresh.
const isNew = d => lastSeen && d.filed_at && d.filed_at > lastSeen;

// ---- filtering: two separate questions, deliberately kept apart. "Does this
// match the search text?" and "is it in the selected wing/room?" The tree needs
// the first only — it must show where matches live across ALL wings, even while
// one wing is selected.
function matchesQuery(d){
  if(!query) return true;
  const q = query.toLowerCase();
  return (d.content + " " + d.id + " " + d.wing + " " + d.room + " " + d.source_file)
           .toLowerCase().includes(q);
}
function matches(d){
  if(sel.wing && d.wing !== sel.wing) return false;
  if(sel.room && d.room !== sel.room) return false;
  return matchesQuery(d);
}

function wireNav(container){
  container.querySelectorAll("[data-nav]").forEach(el => {
    el.onclick = () => {
      const k = el.dataset.nav;
      if(k === "all")       sel = {wing:null, room:null};
      else if(k === "wing") sel = {wing:el.dataset.w, room:null};
      else if(k === "room") sel = {wing:el.dataset.w, room:el.dataset.r};
      render();
    };
  });
}

function renderTree(){
  const pool = DATA.drawers.filter(matchesQuery);
  const wings = {};
  for(const d of pool){
    (wings[d.wing] ??= {rooms:{}, n:0, nu:0});
    wings[d.wing].rooms[d.room] = wings[d.wing].rooms[d.room] || {n:0, nu:0};
    wings[d.wing].rooms[d.room].n++;
    wings[d.wing].n++;
    if(isNew(d)){ wings[d.wing].nu++; wings[d.wing].rooms[d.room].nu++; }
  }
  const names = Object.keys(wings).sort((a,b) => wings[b].n - wings[a].n);
  const t = document.getElementById("tree");
  t.innerHTML = "";
  for(const w of names){
    const W = wings[w];
    const div = document.createElement("div");
    div.className = "wing" + (sel.wing===w ? " open sel" : "");
    div.innerHTML = `<div class="row"><span class="arrow">▶</span>
        <span class="name">${esc(w)}</span>
        ${W.nu ? '<span class="dot" title="'+W.nu+' new"></span>' : ""}
        <span class="n">${W.n}</span></div>
      <div class="rooms">${Object.entries(W.rooms).sort((a,b)=>b[1].n-a[1].n).map(([r,R]) =>
        `<div class="room${sel.wing===w&&sel.room===r?" sel":""}" data-w="${esc(w)}" data-r="${esc(r)}">
           <span class="name">${esc(r)}</span>
           ${R.nu ? '<span class="dot"></span>' : ""}
           <span class="n">${R.n}</span></div>`).join("")}</div>`;
    div.querySelector(".row").onclick = () => {
      sel = (sel.wing===w && !sel.room) ? {wing:null,room:null} : {wing:w, room:null};
      render();
    };
    div.querySelectorAll(".room").forEach(el => el.onclick = e => {
      e.stopPropagation();
      sel = {wing:el.dataset.w, room:el.dataset.r};
      render();
    });
    t.appendChild(div);
  }
}

function renderStats(){
  const s = DATA.storage;
  // Storage only means something when the drawers are on this disk. With a
  // pgvector or qdrant backend they are on another host, so say that rather
  // than print a confident, meaningless number.
  let storeHtml;
  if(!s.available){
    storeHtml = `<div class="st-h">Storage</div>
      <div class="st-note" style="color:var(--fg-subtle)">${esc(s.reason)}</div>`;
  } else {
    const pct = s.disk_total ? (s.disk_used / s.disk_total * 100) : 0;
    const cls = pct > 90 ? "bad" : pct > 75 ? "warn" : "";
    storeHtml = `<div class="st-h">Storage</div>
      <div class="st-row"><span>Palace</span><span>${bytes(s.palace_bytes)}</span></div>
      <div class="st-row"><span>All MemPalace data</span><span>${bytes(s.data_bytes)}</span></div>
      <div class="bar ${cls}"><i style="width:${Math.max(0.6,pct).toFixed(1)}%"></i></div>
      <div class="st-row"><span>${esc(s.mount)}</span><span>${bytes(s.disk_free)} free</span></div>
      <div class="st-note">${pct.toFixed(0)}% of ${bytes(s.disk_total)} used${
          s.dedicated ? " · dedicated volume" : " · <b>shared with the OS</b>"}</div>`;
  }

  // Drawers per month, last 12 months. Twelve bars — enough to see rhythm and
  // gaps, not enough to become a chart. Works on every backend.
  const now = new Date(), keys = [], counts = {};
  for(let i = 11; i >= 0; i--){
    const d = new Date(now.getFullYear(), now.getMonth()-i, 1);
    const k = d.toISOString().slice(0,7);
    keys.push(k); counts[k] = 0;
  }
  for(const d of DATA.drawers){
    const k = (d.filed_at || "").slice(0,7);
    if(k in counts) counts[k]++;
  }
  const max = Math.max(1, ...keys.map(k => counts[k]));
  const mlab = k => new Date(k+"-01").toLocaleDateString(undefined,{month:"short"});

  document.getElementById("stats").innerHTML = storeHtml + `
    <div class="st-h" style="margin-top:13px">Drawers filed · 12mo</div>
    <div class="spark">${keys.map(k =>
      `<i class="${counts[k]?"has":""}" style="height:${counts[k]? Math.max(8,counts[k]/max*100):2}%"
          title="${mlab(k)} ${k.slice(0,4)}: ${counts[k]}"></i>`).join("")}</div>
    <div class="spark-x"><span>${mlab(keys[0])}</span><span>${mlab(keys[11])}</span></div>`;
}

function renderList(){
  const hits = DATA.drawers.filter(matches);
  const c = document.getElementById("content");
  const crumb = `<div class="crumb"><a data-nav="all">All wings</a>`
    + (sel.wing ? ` / <a data-nav="wing" data-w="${esc(sel.wing)}">${esc(sel.wing)}</a>` : "")
    + (sel.room ? ` / ${esc(sel.room)}` : "")
    + ` &nbsp;·&nbsp; ${hits.length} drawer${hits.length===1?"":"s"}${query?` matching “${esc(query)}”`:""}</div>`;
  if(!hits.length){
    c.innerHTML = crumb + `<div class="empty">No drawers match.</div>`;
    wireNav(c); return;
  }
  c.innerHTML = crumb + hits.map(d => {
    let prev = d.content.slice(0,240);
    if(query){
      const k = d.content.toLowerCase().indexOf(query.toLowerCase());
      if(k > 90) prev = "…" + d.content.slice(k-60, k+180);
    }
    return `<div class="card${isNew(d)?" isnew":""}" data-i="${DATA.drawers.indexOf(d)}">
      <div class="top"><span class="loc">${esc(d.wing)} / ${esc(d.room)}</span>
        ${isNew(d) ? '<span class="pill">new</span>' : ""}
        <span class="when">${when(d.filed_at)}</span></div>
      <div class="prev">${hl(prev, query)}</div></div>`;
  }).join("");
  wireNav(c);
  c.querySelectorAll(".card").forEach(el =>
    el.onclick = () => detail(DATA.drawers[+el.dataset.i]));
  c.scrollTop = 0;
}

function detail(d){
  const skip = new Set(["wing","room"]);
  const rows = Object.entries(d.meta).filter(([k]) => !skip.has(k))
    .map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  const c = document.getElementById("content");
  c.innerHTML = `
    <div class="crumb"><a data-back>← back</a> &nbsp;·&nbsp;
      <a data-nav="wing" data-w="${esc(d.wing)}">${esc(d.wing)}</a> /
      <a data-nav="room" data-w="${esc(d.wing)}" data-r="${esc(d.room)}">${esc(d.room)}</a></div>
    <div class="detail"><h2>${esc(d.id)}</h2>
      <dl class="metagrid">${rows}<dt>size</dt><dd>${bytes(d.bytes)}</dd></dl>
      <pre class="doc">${hl(d.content, query)}</pre></div>`;
  wireNav(c);
  c.querySelector("[data-back]").onclick = () => render();
  c.scrollTop = 0;
}

function renderChips(){
  document.getElementById("cntChip").innerHTML = `<b>${DATA.count}</b> drawers`;
  const nu = DATA.drawers.filter(isNew).length;
  const chip = document.getElementById("newChip");
  chip.style.display = nu ? "" : "none";
  chip.textContent = nu + " new";

  // Escape these too. They look trustworthy — one is our own package version,
  // the other comes from PyPI over HTTPS — but "the input is probably fine" is
  // not a security control, and esc() is free.
  const v = DATA.version, vc = document.getElementById("verChip");
  const inst = esc(v.installed), late = esc(v.latest);
  if(v.status === "current"){
    vc.className = "chip act current"; vc.innerHTML = `<b>${inst}</b> · up to date`;
  } else if(v.status === "update-available"){
    vc.className = "chip act update"; vc.innerHTML = `<b>${inst}</b> → ${late} available`;
  } else if(v.status === "ahead"){
    vc.className = "chip act"; vc.innerHTML = `<b>${inst}</b> · ahead of PyPI (${late})`;
  } else if(v.status === "checking"){
    vc.className = "chip act"; vc.innerHTML = `<b>${inst}</b> · checking…`;
  } else if(v.status === "disabled"){
    // We did not look. Say that, rather than implying anything about it.
    vc.className = "chip"; vc.innerHTML = `<b>${inst}</b> · update check off`;
    vc.title = "MPB_CHECK_UPDATES=0 — no outbound requests are made";
  } else {
    // PyPI unreachable. Say so — never imply "current".
    vc.className = "chip act unknown"; vc.innerHTML = `<b>${inst}</b> · update check failed`;
    vc.title = v.error || "could not reach PyPI";
  }
}

function render(){ renderChips(); renderTree(); renderStats(); renderList(); }

async function load(force){
  const btn = document.getElementById("btnRefresh");
  btn.classList.add("spin"); btn.disabled = true;
  try {
    const r = await fetch("/api/data" + (force ? "?refresh=1" : ""));
    if(!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    DATA = d; readAt = d.read_at;
    document.getElementById("palacePath").textContent = d.palace_path;

    const stored = localStorage.getItem(LS_KEY);
    if(stored === null){
      // First ever visit: don't flag all 221 as new. Baseline to the newest
      // drawer so "new" means "arrived since you last looked", from now on.
      lastSeen = d.drawers.length ? d.drawers[0].filed_at : "";
      localStorage.setItem(LS_KEY, lastSeen);
    } else {
      lastSeen = stored;
    }
    render();
    if(force){
      // An explicit Refresh is the "I've seen everything" signal.
      const newest = d.drawers.length ? d.drawers[0].filed_at : lastSeen;
      localStorage.setItem(LS_KEY, newest);
    }
    tick();
  } catch(e){
    document.getElementById("content").innerHTML =
      `<div class="empty err">Failed to load palace:<br>${esc(String(e))}</div>`;
  } finally {
    btn.classList.remove("spin"); btn.disabled = false;
  }
}

document.getElementById("q").addEventListener("input", e => {
  query = e.target.value.trim(); render();
});
document.getElementById("btnRefresh").onclick = () => load(true);
document.getElementById("verChip").onclick = async () => {
  const vc = document.getElementById("verChip");
  vc.textContent = "checking…";
  try {
    DATA.version = await (await fetch("/api/version")).json();
  } catch(e){ /* renderChips will show the failed state */ }
  renderChips();
};
load(false);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    # Fail loudly at startup if the palace is not readable, rather than serving
    # a confident empty browser.
    try:
        d = read_palace(force=True)
    except Exception as e:
        sys.exit(f"ERROR: cannot read palace at {PALACE_PATH}\n  {type(e).__name__}: {e}")

    if d["count"] == 0 and not DEMO:
        sys.exit(
            f"ERROR: palace at {PALACE_PATH} reports 0 drawers.\n"
            "Refusing to start — this is what a wrong path looks like."
        )

    # Warm the PyPI check in the background. The installed version is local and
    # instant; only the comparison needs the network. Blocking the banner on it
    # means an offline user waits out a timeout just to be told their URL.
    if CHECK_UPDATES:
        threading.Thread(target=version_info, kwargs={"force": True}, daemon=True).start()

    s = d["storage"]
    print("=" * 60)
    print("  MemPalace Browser" + ("  [DEMO — synthetic data]" if DEMO else ""))
    print("=" * 60)
    print(f"  palace    : {PALACE_PATH}")
    print(f"  backend   : {BACKEND}")
    print(f"  drawers   : {d['count']}")
    if s.get("available"):
        print(f"  storage   : {s['palace_bytes']/1048576:.1f} MB palace / "
              f"{s['data_bytes']/1048576:.1f} MB total data")
        print(f"  volume    : {s['mount']}  ({s['disk_free']/1073741824:.1f} GB free"
              f"{', dedicated' if s['dedicated'] else ', SHARED WITH OS'})")
    else:
        print(f"  storage   : n/a — {s.get('reason')}")
    if CHECK_UPDATES:
        print(f"  installed : {MP_VERSION}  (checking PyPI in background)")
    else:
        print(f"  installed : {MP_VERSION}  (update check off — no outbound requests)")
    print(f"  serving   : http://{BIND}:{PORT}/")
    print(f"  hosts     : {', '.join(sorted(ALLOWED_HOSTS))}")
    if BIND != "127.0.0.1":
        print(f"  WARNING   : bound to {BIND} — your palace is readable by other")
        print("              machines on this network, with no authentication.")
    print("  Ctrl-C to stop.")
    print("=" * 60)

    with Server((BIND, PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
