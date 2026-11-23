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

import array
import base64
import glob
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config
# Localhost by default: this is a browser for YOUR private palace, and drawers
# routinely hold health, journal and personal material. Binding to all
# interfaces would expose that to every device on your network — including the
# ones you did not think about. Change it deliberately, not by default.
BIND = os.environ.get("MPB_BIND", "127.0.0.1")
PORT = int(os.environ.get("MPB_PORT", "8080"))

# This browser's own version. Deliberately separate from MemPalace's: the chip
# in the header reports MemPalace's version, because that is the number a user
# is actually asking about, and conflating the two is how a browser ends up
# claiming its host library is out of date.
MPB_VERSION = "0.4.0"
REPO_URL = "https://github.com/henderlabs/mempalace-browser"


def _build_id():
    """Fingerprint the source actually running, by hashing this file.

    A hardcoded version string drifts the moment anyone edits without bumping
    it, and then says "0.2.0" about something that is not 0.2.0. A hash of the
    file cannot: it is derived from the thing itself rather than asserted
    alongside it.

    It makes one question answerable from the page that otherwise takes an ssh
    and a shasum: is this box running what the repo says? Compare this against
    `shasum -a 256 app.py` in the checkout. They match or they do not — and
    "the deployment silently drifted from the repo" is exactly the class of
    bug this program exists to not have.

    Deliberately not a git SHA: there is no .git next to the deployment (only
    app.py and run.sh are copied over), and a commit cannot contain its own
    hash anyway.
    """
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        # Never invent one. An unknown build is a fact; a wrong build is a lie.
        return "unknown"


BUILD_ID = _build_id()


def _upgrade_hint():
    """The upgrade command that fits how MemPalace was actually installed.

    Guessing wrong here is worse than staying quiet: `pip install -U` inside a
    uv tool environment is how people break a working install. The interpreter
    path is evidence — run.sh found it by following the `mempalace` console
    script's shebang, so it names the environment MemPalace really lives in.
    """
    p = (sys.executable or "").replace("\\", "/")
    if "/uv/tools/" in p:
        return "uv tool upgrade mempalace"
    if "/pipx/" in p:
        return "pipx upgrade mempalace"
    if "/venv" in p or "/.venv" in p:
        return "pip install -U mempalace   # inside the venv MemPalace lives in"
    return "pip install -U mempalace"

# The HenderLabs mark — three layers, painted bottom to top. Inlined rather
# than read from assets/ because this program is one file with no static dir,
# and a favicon that 404s is worse than none.
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="HenderLabs">
  <rect width="64" height="64" rx="14" fill="#050B14"/>
  <g stroke-linejoin="round" stroke-linecap="round" fill="none">
    <path d="M12 39 L32 50 L52 39" stroke="#FFFFFF" stroke-width="6"/>
    <path d="M12 31 L32 42 L52 31" stroke="#050B14" stroke-width="13"/>
    <path d="M12 31 L32 42 L52 31" stroke="#6E7886" stroke-width="6"/>
    <path d="M32 11 L52 22 L32 33 L12 22 Z" stroke="#050B14" stroke-width="10"/>
    <path d="M32 11 L52 22 L32 33 L12 22 Z" fill="#0D6BFF" stroke="#0D6BFF" stroke-width="4"/>
  </g>
</svg>"""

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

# Host CPU/RAM. Off by default: on a box dedicated to MemPalace these are the
# most useful numbers here, and on a shared box they describe everything except
# your palace. Only the operator knows which they run, so it is a switch rather
# than a guess. MPB_RESOURCES=1 to enable.
RESOURCES = os.environ.get("MPB_RESOURCES", "1" if os.environ.get("MPB_DEMO") == "1" else "0") == "1"

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


# ---------------------------------------------------------------- MPB's own update check
# Tells you whether a newer MemPalace Browser release exists. It NEVER downloads or installs anything: this program
# is read-only and stays that way. Applying an update is update.sh, run by a person, which verifies a published
# checksum, keeps a backup and rolls back if the new version does not come up healthy.
# Disclosed outbound request #2 (the other is the PyPI check): one GET of the releases API, no palace data, a generic
# User-Agent. MPB_CHECK_UPDATES=0 switches BOTH off. MPB_RELEASES_URL overrides the endpoint (forks, air-gap, tests).
# MPB_UPDATE_CHANNEL=prerelease also considers pre-releases. A failed check says "unknown", never "up to date".
RELEASES_URL = os.environ.get("MPB_RELEASES_URL", "https://api.github.com/repos/henderlabs/mempalace-browser/releases/latest")
UPDATE_CHANNEL = "prerelease" if os.environ.get("MPB_UPDATE_CHANNEL", "stable").strip().lower() == "prerelease" else "stable"
MPB_UPDATE_TTL = 6 * 3600
_mpbu = {"at": 0.0, "rel": None, "error": None, "busy": False}
_mpbu_lock = threading.Lock()


def _mpb_vkey(v):
    """Sortable version; dev/rc/beta builds sort BELOW the release they lead up to (0.4.0-dev.9 < 0.4.0)."""
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(dev|rc|alpha|beta|a|b)[.\-]?(\d+)?)?", (v or "").strip(), re.I)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), 0 if m.group(4) else 1, int(m.group(5) or 0))


def _shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _mpb_update_cmd():
    """The command a person runs to apply an update, matched to how this copy is run."""
    if os.path.exists("/.dockerenv"):
        return "docker compose pull && docker compose up -d   # in the folder that holds your compose file"
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "update.sh")
    if not os.path.exists(script):
        return "# update.sh is not next to app.py. Download it from the release page, put it beside app.py, then: bash update.sh"
    unit = _service_unit()
    env = ("MPB_PORT=%s " % PORT) if str(PORT) != "8080" else ""
    return env + "bash " + _shq(script) + ((" --unit " + _shq(unit)) if unit else "   # then restart this browser")


def _mpb_fetch_release():
    url = RELEASES_URL
    if UPDATE_CHANNEL == "prerelease" and url.endswith("/releases/latest"):
        url = url[: -len("/latest")] + "?per_page=10"
    req = urllib.request.Request(url, headers={"User-Agent": "mempalace-browser", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        data = json.load(r)
    rels = data if isinstance(data, list) else [data]
    rel = next((x for x in rels if isinstance(x, dict) and not x.get("draft")
                and (UPDATE_CHANNEL == "prerelease" or not x.get("prerelease"))), None)
    if not rel or not rel.get("tag_name"):
        raise ValueError("no release found")
    page = str(rel.get("html_url") or "")
    return {"tag": str(rel["tag_name"])[:40], "url": page if page.startswith("https://") else "",
            "published": str(rel.get("published_at") or "")[:10], "prerelease": bool(rel.get("prerelease"))}


def _mpb_refresh():
    try:
        rel, err = _mpb_fetch_release(), None
    except Exception as e:
        rel, err = None, ("%s: %s" % (type(e).__name__, e))[:120]
    with _mpbu_lock:
        _mpbu.update(at=time.time(), rel=rel, error=err, busy=False)


def mpb_update_info(force=False):
    base = {"current": MPB_VERSION, "channel": UPDATE_CHANNEL, "cmd": _mpb_update_cmd(),
            "latest": None, "url": "", "published": "", "error": None, "checked_at": None}
    if not CHECK_UPDATES or DEMO:
        return dict(base, status="disabled")
    if force:
        _mpb_refresh()
    else:
        with _mpbu_lock:
            ttl = MPB_UPDATE_TTL if _mpbu["rel"] else 900          # retry a failed check after 15 minutes
            start = (time.time() - _mpbu["at"]) > ttl and not _mpbu["busy"]
            if start:
                _mpbu["busy"] = True
        if start:
            threading.Thread(target=_mpb_refresh, daemon=True).start()
    with _mpbu_lock:
        at, rel, err = _mpbu["at"], _mpbu["rel"], _mpbu["error"]
    if not at:
        return dict(base, status="checking")
    base["checked_at"] = datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds")
    if rel is None:
        if err and "HTTP Error 404" in err:          # the endpoint answered: no release is published (yet)
            return dict(base, status="none")
        return dict(base, status="unknown", error=err)
    kc, kl = _mpb_vkey(MPB_VERSION), _mpb_vkey(rel["tag"])
    if kc is None or kl is None:
        return dict(base, status="unknown", error="cannot compare versions", latest=rel["tag"].lstrip("v"))
    status = "update-available" if kl > kc else ("current" if kl == kc else "ahead")
    return dict(base, status=status, latest=rel["tag"].lstrip("v"), url=rel["url"], published=rel["published"])


if CHECK_UPDATES and not DEMO:
    _mpbu["busy"] = True           # start the first check in the background so the page does not wait for the network
    threading.Thread(target=_mpb_refresh, daemon=True).start()


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


def resource_info():
    """Host CPU load and memory, when the operator asks for them.

    Off by default, MPB_RESOURCES=1 to enable. Whether these numbers mean
    anything depends entirely on the deployment: on a box dedicated to
    MemPalace they are the most useful figures on the page, because the
    embedder loads a model into RAM and Chroma keeps its HNSW index there —
    RAM is what runs out before disk does as a palace grows. On a shared box
    they describe everything except your palace. Only the operator knows which
    they have, so it is their switch, not our guess.

    Reported as the HOST, never as MemPalace. Attributing RSS to MemPalace
    would mean hunting processes and guessing which is which; "this machine is
    using 2.1 of 3.8 GB" is true, "MemPalace is using 2.1 GB" would not be.

    Linux only — /proc is the whole implementation, and there is no stdlib way
    to do this portably. Elsewhere it reports unavailable rather than zero.
    """
    if not RESOURCES:
        return {"enabled": False}
    if DEMO:
        return {"enabled": True, "available": True, "mem_total": 4_007_657_472,
                "mem_used": 1_331_691_520, "mem_avail": 2_675_965_952,
                "load1": 0.14, "cpus": 2, "swap_total": 0, "swap_used": 0}
    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                mem[k.strip()] = int(v.strip().split()[0]) * 1024   # kB -> bytes
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        swt = mem.get("SwapTotal", 0)
        with open("/proc/loadavg") as fh:
            load1 = float(fh.read().split()[0])
        return {
            "enabled": True, "available": True,
            "mem_total": total, "mem_avail": avail, "mem_used": total - avail,
            "swap_total": swt, "swap_used": swt - mem.get("SwapFree", 0),
            "load1": load1, "cpus": os.cpu_count() or 1,
        }
    except (OSError, ValueError, IndexError) as e:
        # No /proc, or a shape we do not recognise. Unavailable is a fact.
        return {"enabled": True, "available": False,
                "reason": f"host metrics unavailable on this platform ({type(e).__name__})"}


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
                "mount": "/srv/demo-palace", "dedicated": True,
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


# ---------------------------------------------------------------- operations
# "Can I trust what I am looking at?" Which service this is, whether the
# palace's SQLite layer passes MemPalace's own integrity probe, and how fresh the
# newest backup is. None of this reads chroma.sqlite3 directly: integrity goes
# through MemPalace's read-only probe (repair.sqlite_integrity_status) and
# backups are only stat()ed, never opened. Nothing here writes anywhere.
#
# Backups are found by glob because only the operator knows where they live.
# Unset, the panel says so rather than guessing. Entries that differ only by a
# .tar.gz/.tgz/.tar/.zip suffix are one backup (folder plus its tarball).
BACKUP_GLOBS = [g.strip() for g in os.environ.get("MPB_BACKUP_GLOBS", "").split(",") if g.strip()]
INTEGRITY_TTL = 300       # seconds; the probe opens the database, so not every read
BACKUP_TTL = 60
_STARTED_AT = time.time()
_ops_cache = {"integrity": (0.0, None), "backups": (0.0, None)}
_ops_lock = threading.Lock()


def _service_unit():
    """The systemd unit this process runs under, or None when started by hand."""
    try:
        with open("/proc/self/cgroup") as fh:
            for line in fh:
                leaf = line.strip().rsplit("/", 1)[-1]
                if leaf.endswith(".service"):
                    return leaf
    except OSError:
        pass
    return None


def _whoami():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER") or ""


def identity_info():
    return {
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "user": _whoami(),
        "service": _service_unit(),
        "started": _STARTED_AT,
        "bind": BIND,
        "port": PORT,
    }


def integrity_info(force=False):
    """SQLite quick_check verdict from MemPalace's own probe, cached briefly."""
    if DEMO:
        return {"available": True, "checked": True, "errors": [], "reason": "",
                "checked_at": time.time()}
    if BACKEND not in LOCAL_BACKENDS:
        return {"available": False,
                "reason": f"{BACKEND} backend — no local SQLite file to check"}
    with _ops_lock:
        at, cached = _ops_cache["integrity"]
        if cached and not force and time.time() - at < INTEGRITY_TTL:
            return cached
    try:
        from mempalace.repair import sqlite_integrity_status
        st = sqlite_integrity_status(PALACE_PATH)
        info = {"available": True, "checked": bool(st.checked),
                "errors": [str(e)[:240] for e in list(st.errors)[:5]],
                "reason": st.reason or "", "checked_at": time.time()}
    except Exception as e:
        # Unavailable is a fact. Never report "ok" for a check that did not run.
        info = {"available": False,
                "reason": f"integrity probe unavailable ({type(e).__name__}: {e})"[:240]}
    with _ops_lock:
        _ops_cache["integrity"] = (time.time(), info)
    return info


def backups_info(force=False):
    """Freshness of the newest backup matching MPB_BACKUP_GLOBS (stat only)."""
    if DEMO:
        now = time.time()
        return {"configured": True, "count": 3, "dir": "/srv/demo-backups",
                "newest": {"name": "demo-backup-20260101", "mtime": now - 3 * 3600,
                           "bytes": 8_400_000}}
    if not BACKUP_GLOBS:
        return {"configured": False}
    with _ops_lock:
        at, cached = _ops_cache["backups"]
        if cached and not force and time.time() - at < BACKUP_TTL:
            return cached
    entries = {}
    for pattern in BACKUP_GLOBS:
        for p in glob.glob(os.path.expanduser(pattern)):
            try:
                st = os.stat(p)
            except OSError:
                continue
            name = os.path.basename(p.rstrip("/"))
            key = re.sub(r"\.(tar\.gz|tgz|tar|zip)$", "", name)
            cur = entries.get(key)
            if cur is None or st.st_mtime > cur["mtime"]:
                entries[key] = {"name": name, "path": p, "mtime": st.st_mtime,
                                "is_dir": os.path.isdir(p), "size": st.st_size}
    if not entries:
        info = {"configured": True, "count": 0}
    else:
        newest = max(entries.values(), key=lambda e: e["mtime"])
        size = _dir_size(newest["path"])[0] if newest["is_dir"] else newest["size"]
        info = {"configured": True, "count": len(entries),
                "dir": os.path.dirname(newest["path"].rstrip("/")),
                "newest": {"name": newest["name"], "mtime": newest["mtime"], "bytes": size}}
    with _ops_lock:
        _ops_cache["backups"] = (time.time(), info)
    return info


def ops_info():
    return {"identity": identity_info(), "integrity": integrity_info(),
            "backups": backups_info(), "now": time.time()}


# ---------------------------------------------------------------- secrets
# Read-only scan of drawer text for credential-SHAPED strings. A heuristic, not a
# verdict: it says "this looks like a key", never "this key is live". Patterns are
# deliberately generic (provider key shapes and "label: value" pairs); anything
# tuned to one person's environment belongs in a private fork, not here.
#
# Findings are MASKED before they leave this function. The warnings MPB shows
# must never become a second copy of the secret they are warning about. (Opening
# a drawer still shows its full text, as it always has.)
#
# MPB_SECRET_SCAN=0 turns the scan off. MPB_SECRET_ALLOW=<id>[,<id>...] stops
# flagging specific drawers (false positives); it hides them from this scan only.
SECRET_SCAN = os.environ.get("MPB_SECRET_SCAN", "1") != "0"
SECRET_ALLOW = {s.strip() for s in os.environ.get("MPB_SECRET_ALLOW", "").split(",") if s.strip()}
MAX_SECRET_FINDINGS = 100
MAX_PER_DRAWER = 3

# (kind, pattern, group): provider shapes need no label, the shape is the evidence.
_SECRET_SHAPES = [
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"), 0),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), 0),
    ("API key (sk-)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), 0),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("Private key block",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----"), 0),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), 0),
    ("Bearer token", re.compile(r"(?i)\bBearer[ \t]+([A-Za-z0-9._~+/=-]{24,})"), 1),
    ("Password in URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:([^\s/@|=]{3,})@"), 1),
    ("Notion token", re.compile(r"\bntn_[A-Za-z0-9]{20,}\b"), 0),
    ("Proxmox API token", re.compile(r"\b[\w.-]+@[a-z]+![\w-]+=[0-9a-fA-F-]{32,}\b"), 0),
]
# "label: value" or "label=value" where the label names a credential.
_SECRET_LABELED = re.compile(
    r"(?i)\b(access[ _-]?key(?:[ _-]?id)?|secret[ _-]?(?:access[ _-]?)?key|api[ _-]?key|"
    r"(?:auth|access|refresh)[ _-]?token|client[ _-]?secret|passw(?:or)?d|passphrase|secret|token)"
    r"\b[ \t]*[:=][ \t]*[\"']?([^\s\"'|;<>]{8,})")
_PLACEHOLDER_WORDS = ("redact", "removed", "placeholder", "example", "changeme", "your", "xxxx",
                      "****", "none", "null", "unknown", "stored", "required", "hidden")


def _plausible_secret(v):
    """Reject prose, placeholders and paths; keep things that look like a credential."""
    v = v.rstrip(".,;:")
    if len(v) < 8:
        return None
    low = v.lower()
    if any(w in low for w in _PLACEHOLDER_WORDS):
        return None
    if low.startswith(("http", "/", "$", "{", "~", "<")) or "{{" in v or "${" in v:
        return None
    # A credential has both letters and digits; a word or a number alone is not one.
    if not (re.search(r"[0-9]", v) and re.search(r"[A-Za-z]", v)):
        return None
    return v


def _mask_secret(v):
    return (v[:4] + "…" + v[-2:]) if len(v) >= 12 else (v[:2] + "…")


# A token ID (user@realm!tokenname) is a NAME, not a secret: on its own it cannot log in. But it
# tells an attacker which account to aim at, and it is the half of a credential an old note often
# keeps after the secret half is gone. So it is reported at INFO level, separately from secrets:
# no "secret?" badge, not counted in the secret totals, nothing masked in the drawer itself.
_TOKEN_ID = re.compile(r"\b([\w.-]+)@([a-z]+)!([\w-]+)")
_TOKEN_ID_PLACEHOLDERS = {"user", "username", "realm", "tokenname", "tokenid", "token", "name", "id", "example"}


def scan_token_ids(drawers):
    out, seen_drawers = [], set()
    for d in drawers:
        text = d.get("content") or ""
        if "!" not in text or "@" not in text or d["id"] in SECRET_ALLOW:
            continue
        for m in _TOKEN_ID.finditer(text):
            user, realm, name = m.group(1), m.group(2), m.group(3)
            if user.lower() in _TOKEN_ID_PLACEHOLDERS or realm.lower() in _TOKEN_ID_PLACEHOLDERS \
                    or name.lower() in _TOKEN_ID_PLACEHOLDERS:
                continue
            if d["id"] in seen_drawers:
                break
            seen_drawers.add(d["id"])
            out.append({"id": d["id"], "wing": d.get("wing", ""), "room": d.get("room", ""),
                        "masked": _mask_secret(m.group(0))})
            break
    return out[:50]


def scan_secrets(drawers):
    if not SECRET_SCAN:
        return {"enabled": False}
    empty = {"enabled": True, "scanned": len(drawers), "total": 0, "drawers": 0,
             "by_kind": {}, "findings": [], "ids": [], "allowed": 0}
    if DEMO:
        return empty
    findings, by_kind, ids, allowed, total = [], Counter(), [], 0, 0
    for d in drawers:
        text = d.get("content") or ""
        if not text:
            continue
        hits = []
        for kind, rx, g in _SECRET_SHAPES:
            for m in rx.finditer(text):
                v = m.group(g)
                if kind == "Password in URL" and v.isdigit():
                    continue          # host:port, not user:password
                hits.append((kind, v, m.start(g)))
        for m in _SECRET_LABELED.finditer(text):
            val = _plausible_secret(m.group(2))
            if val:
                hits.append(("Labeled credential", val, m.start(2)))
        if not hits:
            continue
        if d["id"] in SECRET_ALLOW:
            allowed += 1
            continue
        seen, kept = set(), 0
        for kind, val, pos in hits:
            if val in seen:
                continue
            seen.add(val)
            kept += 1
            if kept > MAX_PER_DRAWER:
                break
            ls = text.rfind("\n", 0, pos) + 1
            le = text.find("\n", pos)
            line = text[ls:(len(text) if le < 0 else le)]
            # Mask EVERY value found in this drawer, not just this one: the context
            # line may carry a second secret, and a warning must never print one.
            # Longest first, so a secret that contains a shorter one cannot leak a fragment.
            for v2 in sorted({h[1] for h in hits if not h[1].startswith("-----BEGIN")},
                             key=len, reverse=True):
                line = line.replace(v2, _mask_secret(v2))
            ctx = line.strip()
            if len(ctx) > 150:
                ctx = ctx[:150] + "…"
            total += 1
            by_kind[kind] += 1
            if d["id"] not in ids:
                ids.append(d["id"])
            if len(findings) < MAX_SECRET_FINDINGS:
                findings.append({"id": d["id"], "wing": d.get("wing", ""), "room": d.get("room", ""),
                                 "kind": kind,
                                 "masked": "PEM header" if kind == "Private key block" else _mask_secret(val),
                                 "ctx": ctx})
    return {"enabled": True, "scanned": len(drawers), "total": total, "drawers": len(ids),
            "by_kind": dict(by_kind), "findings": findings, "ids": ids[:500], "allowed": allowed,
            "tokenids": scan_token_ids(drawers)}


# ---------------------------------------------------------------- audit
# MemPalace's own organisation audit (rooms, naming, tunnels, hallways, knowledge
# graph), imported as a library function so MPB cannot disagree with `mempalace audit`.
# Read-only by construction (the package says so). Cached briefly; it is fast.
AUDIT_TTL = 60
_audit_cache = (0.0, None)


def audit_info(force=False):
    global _audit_cache
    if DEMO:
        zero = {"total": 0, "never_traversed": 0}
        return {"available": True, "report": {
            "scores": {"overall": 82, "rooms": 98, "naming": 90, "tunnels": 100,
                       "hallways": None, "knowledge_graph": 60},
            "rooms": {"total_drawers": 0, "generic_drawers": 0, "flat_wings": []},
            "naming": {"wing_drift": [], "room_drift": [], "mixed_wings": [],
                       "wing_prefix_pairs": [], "tiny_wings": []},
            "tunnels": zero, "hallways": {"total": 0},
            "knowledge_graph": {"current_facts": 0, "distinct_predicates": 0, "one_off_predicates": 0,
                                "one_off_sample": []},
            "findings": []}}
    at, cached = _audit_cache
    if cached and not force and time.time() - at < AUDIT_TTL:
        return cached
    try:
        from mempalace.palace_audit import audit_palace
        rep = audit_palace(PALACE_PATH)
        info = {"available": True, "report": json.loads(json.dumps(rep, default=str))}
    except Exception as e:
        info = {"available": False,
                "reason": f"audit unavailable ({type(e).__name__}: {e})"[:240]}
    _audit_cache = (time.time(), info)
    return info


# ---------------------------------------------------------------- galaxy
# The Galaxy view needs one thing from the server: each drawer's STORED embedding vector.
# They come through MemPalace's own collection API (the same call that reads the drawers), so no
# embedding model is loaded and nothing is written. The browser does the layout. Vectors are sent as
# float32 bytes (base64) in the same order as `ids`; the page joins them to the drawers by id.
GALAXY_TTL = 300
_galaxy_cache = (0.0, None)


def galaxy_info():
    global _galaxy_cache
    at, cached = _galaxy_cache
    if cached and time.time() - at < GALAXY_TTL:
        return cached
    arr = array.array("f")
    ids = []
    if DEMO:
        # Synthetic clusters, one centre per wing: real vectors are never shown in a demo.
        import random
        rng = random.Random(7)
        dim, centres = 48, {}
        for d in _demo_drawers():
            c = centres.setdefault(d["wing"], [rng.gauss(0, 1) for _ in range(dim)])
            arr.extend([c[k] + rng.gauss(0, 0.9) for k in range(dim)])
            ids.append(d["id"])
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            col = get_collection(PALACE_PATH, collection_name=COLLECTION, create=False)
            res = col.get(include=["embeddings"])
        emb = res.embeddings
        if emb is None or len(emb) == 0:
            raise RuntimeError("this palace returned no stored embeddings")
        dim = len(emb[0])
        for did, v in zip(res.ids, emb):
            if v is None or len(v) != dim:
                continue
            arr.extend([float(x) for x in v])
            ids.append(did)
    if sys.byteorder == "big":
        arr.byteswap()
    info = {"ids": ids, "dim": dim, "vectors": base64.b64encode(arr.tobytes()).decode("ascii")}
    _galaxy_cache = (time.time(), info)
    return info


# ---------------------------------------------------------------- revisions
# Every time a drawer is edited, MemPalace appends its PRIOR text to a revision log. That log is
# importable through the package (mempalace.revisions.load_drawer_revisions), so MPB can show a
# drawer's history without reading any file itself. Old text can hold credentials a redaction
# removed from the current drawer, so previews are MASKED with the same rules as the secret scan.
REV_TTL = 60
_rev_cache = (0.0, None)


def mask_secrets(text):
    """Replace every credential-shaped value in `text` with a masked form. Returns (masked, count)."""
    hits = {}
    for kind, rx, g in _SECRET_SHAPES:
        for m in rx.finditer(text):
            v = m.group(g)
            if kind == "Password in URL" and v.isdigit():
                continue
            if not v.startswith("-----BEGIN"):
                hits[v] = kind
    for m in _SECRET_LABELED.finditer(text):
        v = _plausible_secret(m.group(2))
        if v and v not in hits:
            hits[v] = "Labeled credential"
    out = text
    for v in sorted(hits, key=len, reverse=True):
        out = out.replace(v, _mask_secret(v))
    return out, len(hits)


def _load_revisions(drawer_id=None):
    from mempalace.revisions import load_drawer_revisions
    return load_drawer_revisions(PALACE_PATH, drawer_id)


def revisions_for(drawer_id):
    rows = _load_revisions(drawer_id)              # oldest first; each row is the text that was REPLACED
    try:
        cur_h = _current_hashes([d for d in read_palace()["drawers"] if _parent_of(d) == drawer_id]).get(drawer_id)
    except Exception:
        cur_h = None
    out, rewrites = [], 0
    for r in rows:
        text = r.get("content") or ""
        same = cur_h is not None and hashlib.md5(_norm_txt(text).encode()).hexdigest() == cur_h
        rewrites += 0 if same else 1
        masked, n = mask_secrets(text)
        out.append({"revision_id": r.get("revision_id"), "recorded_at": r.get("recorded_at"), "same": same,
                    "chars": len(text), "secrets": n, "preview": masked[:260], "full": masked[:6000]})
    return {"id": drawer_id, "count": len(rows), "rewrites": rewrites, "revisions": out[-20:]}


def revision_secrets(force=False):
    """Credential-shaped text that survives only in OLD versions (the current drawers are clean)."""
    global _rev_cache
    at, cached = _rev_cache
    if cached and not force and time.time() - at < REV_TTL:
        return cached
    if DEMO or not SECRET_SCAN:
        info = {"enabled": False, "versions": 0, "drawers": []}
    else:
        try:
            rows = _load_revisions(None)
            ids, versions, revs = [], 0, []
            for r in rows:
                did, at_ = r.get("drawer_id"), str(r.get("recorded_at") or "")
                if did:                                   # kept as (drawer, time, hash): compared with the CURRENT text later
                    revs.append((did, at_, hashlib.md5(_norm_txt(r.get("content")).encode()).hexdigest()))
                if r.get("drawer_id") in SECRET_ALLOW:
                    continue
                _m, n = mask_secrets(r.get("content") or "")
                if n:
                    versions += 1
                    if r["drawer_id"] not in ids:
                        ids.append(r["drawer_id"])
            info = {"enabled": True, "versions": versions, "drawers": ids[:200], "scanned": len(rows), "_revs": revs}
        except Exception as e:
            info = {"enabled": True, "error": f"{type(e).__name__}: {e}"[:200], "versions": 0, "drawers": []}
    _rev_cache = (time.time(), info)
    return info


def _norm_txt(t):
    return re.sub(r"\s+", "", t or "")


def _parent_of(d):
    return (d.get("meta") or {}).get("parent_drawer_id") or str(d["id"]).rsplit("_chunk_", 1)[0]


def _current_hashes(drawers):
    """Hash of each parent drawer's CURRENT text (chunks in order, whitespace ignored)."""
    by = defaultdict(list)
    for d in drawers:
        by[_parent_of(d)].append(d)
    out = {}
    for pid, ch in by.items():
        ch.sort(key=lambda d: d["chunk_index"] if isinstance(d.get("chunk_index"), int) else 0)
        out[pid] = hashlib.md5("".join(_norm_txt(d["content"]) for d in ch).encode()).hexdigest()
    return out


def revision_info(drawers):
    """revision_secrets() plus per-drawer REWRITE stats. A revision whose old text equals the current text was written by a
    move (wing/room change) or a re-chunk, so it is not counted as a rewrite."""
    info = revision_secrets()
    revs = info.get("_revs") or []
    cur = _current_hashes(drawers) if revs else {}
    stats, moved = {}, 0
    for did, at_, h in revs:
        if cur.get(did) == h:
            moved += 1
            continue
        st = stats.setdefault(did, [0, ""])
        st[0] += 1
        if at_ > st[1]:
            st[1] = at_
    out = {k: v for k, v in info.items() if k != "_revs"}
    out["stats"], out["moves"] = stats, moved
    return out


# ---------------------------------------------------------------- documents
def _chunk_sort_key(d):
    """Order chunks within a document.

    chunk_index is scoped to (source_file, ROOM), not to the file: the miner
    restarts numbering for every room a file's chunks land in. So room comes
    first in the key, and a multi-room document reads as one contiguous run per
    room rather than seven interleaved sequences that all start at zero.

    The remaining fallbacks handle a field that is absent on some drawers and
    genuinely duplicated on others. An arbitrary but STABLE order beats a
    different order on every reload.
    """
    ci = d.get("chunk_index")
    return (d.get("room") or "", ci is None, ci if isinstance(ci, int) else 0,
            d.get("filed_at") or "", d["id"])


def build_documents(drawers):
    """Group chunks back into the file they were mined from.

    A drawer is a ~800-character slice. The thing a person actually wrote is
    the file, so reading a 27-chunk document as 27 fragments — each starting
    mid-sentence — makes real content look like corrupted data.

    source_file is treated as an OPAQUE key, because it is free text rather
    than a path: it holds absolute paths, bare filenames, and prose labels
    ("telegram conversation"). Two spellings of one file therefore stay two
    documents. That is deliberate — merging them means guessing, and a wrong
    merge silently fuses two people's notes into one.

    Documents are NOT a level of the wing/room tree. The miner files chunks by
    topic, so one file's chunks legitimately scatter across rooms (in the
    palace this was built against, workbench.md spans seven). A document is a
    lens over drawers, never a branch of them.
    """
    groups = defaultdict(list)
    for d in drawers:
        if d.get("source_file"):          # "" and None are both "no document"
            groups[d["source_file"]].append(d)

    docs = []
    for key, items in groups.items():
        if len(items) < 2:
            # A one-chunk group is just a drawer wearing a document costume.
            continue
        items = sorted(items, key=_chunk_sort_key)

        # Say when the order is not trustworthy — but check it per ROOM, because
        # that is the scope the miner numbers in. Checking chunk_index across a
        # whole file reports a duplicate for every multi-room document, which is
        # the miner working exactly as designed: a 27-chunk file spanning seven
        # rooms legitimately has seven chunks numbered 0. Flagging that would
        # cry wolf on the healthy case and bury the real one.
        issues = []
        unnumbered = sum(1 for d in items if d.get("chunk_index") is None)
        if unnumbered:
            issues.append("%d chunk(s) carry no chunk_index" % unnumbered)

        # chunk_index numbers the chunks INSIDE one drawer and restarts at 0 in the next drawer, so two drawers from the
        # same source are not a duplicate. Count per (room, drawer): a repeated number inside one drawer is the real fault.
        per_room = defaultdict(list)
        for d in items:
            if isinstance(d.get("chunk_index"), int):
                pid = (d.get("meta") or {}).get("parent_drawer_id") or str(d["id"]).rsplit("_chunk_", 1)[0]
                per_room[(d["room"], pid)].append(d["chunk_index"])
        dupe_rooms, gap_rooms = set(), set()
        for (rm, _pid), idx in per_room.items():
            if len(set(idx)) != len(idx):
                dupe_rooms.add(rm)
            elif any(y - x != 1 for x, y in zip(sorted(idx), sorted(idx)[1:])):      # a gap INSIDE one drawer; a fragment that starts at 3 is not a gap
                gap_rooms.add(rm)
        if dupe_rooms:
            # Two drawers claiming the same chunk of the same room means this
            # content was filed more than once. Stating what the data shows,
            # not why — the cause is upstream's business, but a reader deserves
            # to know some of what follows is duplicated.
            issues.append("filed more than once — duplicate chunk numbers in %s"
                          % ", ".join(sorted(dupe_rooms)))
        if gap_rooms:
            issues.append("chunk numbering has gaps in %s" % ", ".join(sorted(gap_rooms)))

        filed = sorted(d["filed_at"] for d in items if d["filed_at"])
        docs.append({
            "key": key,
            "n": len(items),
            "ids": [d["id"] for d in items],
            "wings": sorted({d["wing"] for d in items}),
            "rooms": sorted({d["room"] for d in items}),
            "bytes": sum(d["bytes"] for d in items),
            "first": filed[0] if filed else "",
            "last": filed[-1] if filed else "",
            "issues": issues,
        })
    docs.sort(key=lambda x: (-x["n"], x["key"]))
    return docs


# ---------------------------------------------------------------- layers
def _layer_counts(drawers):
    """Count every layer a palace advertises — including the empty ones.

    This is the panel this program exists to make honest. MemPalace offers
    closets, halls, entities, hallways, tunnels and a knowledge graph, and in
    a typical palace most of them have never been written to. Nothing tells
    you that: an empty knowledge graph answers kg_query with count:0, which
    reads as "there are no such facts" rather than "this layer does not
    exist". Showing the coverage is the whole difference.

    Every probe is individually guarded: a layer we cannot read reports
    "unavailable", never zero. Zero and unknown are different claims.
    """
    total = len(drawers) or 1
    halls = Counter(d["meta"].get("hall") for d in drawers if d["meta"].get("hall"))
    ents = sum(1 for d in drawers if d["meta"].get("entities"))

    # Passive tunnels: the same room name appearing in two or more wings.
    # Computed here from metadata we already hold rather than calling
    # graph_stats(), which would re-read the entire collection to learn what
    # is already in memory. Note the match is exact-string and case-sensitive,
    # which is MemPalace's own behaviour — `Decisions` does not bridge to
    # `decisions`. Mirroring that is the point; "fixing" it here would report
    # tunnels the palace itself does not have.
    rooms_to_wings = defaultdict(set)
    for d in drawers:
        rooms_to_wings[d["room"]].add(d["wing"])
    passive = sum(1 for w in rooms_to_wings.values() if len(w) > 1)

    out = {
        "drawers": {"n": len(drawers), "pct": 100},
        "halls": {"n": sum(halls.values()), "pct": round(sum(halls.values()) / total * 100),
                  "values": halls.most_common()},
        "entities": {"n": ents, "pct": round(ents / total * 100)},
        "tunnels_passive": {"n": passive},
    }

    if DEMO:
        out["closets"] = {"n": 2}
        out["hallways"] = {"n": 0}
        out["tunnels_explicit"] = {"n": 0}
        out["kg"] = {"entities": 0, "triples": 0}
        return out

    try:
        cc = get_collection(PALACE_PATH, collection_name="mempalace_closets", create=False)
        out["closets"] = {"n": cc.count()}
    except Exception as e:
        out["closets"] = {"error": str(e)[:80]}

    try:
        from mempalace.hallways import list_hallways
        out["hallways"] = {"n": len(list_hallways())}
    except Exception as e:
        out["hallways"] = {"error": str(e)[:80]}

    try:
        from mempalace.palace_graph import list_tunnels
        out["tunnels_explicit"] = {"n": len(list_tunnels())}
    except Exception as e:
        out["tunnels_explicit"] = {"error": str(e)[:80]}

    # ALWAYS report which file this came from. There is more than one candidate
    # and they disagree: mcp_server._resolve_kg_path() returns
    # <palace_path>/knowledge_graph.sqlite3 when the server was started with an
    # explicit --palace, and knowledge_graph.DEFAULT_KG_PATH (~/.mempalace/...)
    # otherwise. Those are different files even when --palace names the default
    # path, so an agent and this browser can read the same palace and give
    # different answers about its graph. KnowledgeGraph() creates a missing file
    # rather than failing, so the losing side reports a confident zero instead
    # of an error. A count without its path is not an answer.
    try:
        from mempalace.knowledge_graph import KnowledgeGraph, DEFAULT_KG_PATH
        kg = KnowledgeGraph()
        try:
            s = kg.stats() or {}
            out["kg"] = {"entities": s.get("entities", 0),
                         "triples": s.get("triples", 0),
                         "path": DEFAULT_KG_PATH}
        finally:
            kg.close()
        alt = os.path.join(PALACE_PATH, "knowledge_graph.sqlite3")
        if os.path.realpath(alt) != os.path.realpath(DEFAULT_KG_PATH) and os.path.exists(alt):
            out["kg"]["rival"] = alt
    except Exception as e:
        out["kg"] = {"error": str(e)[:80]}

    return out


# ---------------------------------------------------------------- palace read
_palace_cache = {"at": 0.0, "data": None}
_palace_lock = threading.Lock()

# MemPalace raises a palace's warnings exactly ONCE per process — the second
# get_collection() for the same collection is silent. Our read is cached for
# PALACE_TTL, so capturing per-read means the warning shows for thirty seconds
# and then the status goes green and stays green: a health check that heals
# itself by forgetting. Latch them instead. The condition (e.g. no recorded
# embedder identity) is a property of the palace, not of one read.
#
# It deliberately cannot un-latch. The same once-per-process rule means we
# could not observe the fix either, and a warning that clears itself without
# evidence is the bug, not the feature. Restart to re-evaluate.
_palace_notes_seen = []


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
    seq = Counter()
    for wing, room, days_ago, src, content in spec:
        filed = (now - timedelta(days=days_ago)).isoformat(timespec="microseconds")
        # Number the chunks per source file, the way a real mine does — it is
        # what makes the Documents view demonstrate anything.
        ci = seq[src]
        seq[src] += 1
        out.append({
            "id": f"drawer_{wing}_{room}_{abs(hash((wing, room, content))) % (16**24):024x}",
            "wing": wing, "room": room, "filed_at": filed,
            "added_by": "demo", "source_file": src, "chunk_index": ci,
            "content": content, "bytes": len(content.encode("utf-8")),
            "meta": {"wing": wing, "room": room, "filed_at": filed,
                     "added_by": "demo", "source_file": src, "chunk_index": str(ci)},
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
                "ops": ops_info(),
                "secrets": scan_secrets(drawers),
                "revsecrets": revision_info(drawers),
                "phash": _current_hashes(drawers),
                "audit": audit_info(),
                "resources": resource_info(),
                "documents": build_documents(drawers),
                "layers": _layer_counts(drawers),
                "palace_notes": [],
                "mpb_version": MPB_VERSION,
                "build_id": BUILD_ID,
                "repo_url": REPO_URL,
                "upgrade_cmd": _upgrade_hint(),
                "mpb_update": mpb_update_info(),
                "demo": True,
            }
            _palace_cache.update(at=now, data=data)
            return data

        # Capture warnings MemPalace raises while opening the collection. The
        # embedder-identity warning is the one that matters: it goes to stderr
        # and nowhere else, so anyone who started this detached never sees it —
        # yet it means the palace has no recorded embedder, and if the default
        # model ever changes, new embeddings silently mismatch every drawer
        # already filed. That is palace health, and it belongs on screen.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            col = get_collection(
                PALACE_PATH,
                collection_name=COLLECTION,
                create=False,            # never manufacture a palace
            )
            for w in caught:
                msg = str(w.message)[:240]
                if msg not in _palace_notes_seen:
                    _palace_notes_seen.append(msg)

        res = col.get(include=["metadatas", "documents"])
        ids = list(res.ids or [])
        metas = list(res.metadatas or [])
        docs = list(res.documents or [])

        drawers = []
        for i, did in enumerate(ids):
            m = dict(metas[i] or {})
            ci = m.get("chunk_index")
            drawers.append({
                "id": did,
                "wing": m.get("wing") or "(unfiled)",
                "room": m.get("room") or "(none)",
                "filed_at": m.get("filed_at") or "",
                "added_by": m.get("added_by") or "",
                "source_file": m.get("source_file") or "",
                "chunk_index": ci if isinstance(ci, int) else None,
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
            "ops": ops_info(),
            "secrets": scan_secrets(drawers),
            "revsecrets": revision_info(drawers),
            "phash": _current_hashes(drawers),
            "audit": audit_info(),
            "resources": resource_info(),
            "documents": build_documents(drawers),
            "layers": _layer_counts(drawers),
            "palace_notes": list(_palace_notes_seen),
            "mpb_version": MPB_VERSION,
            "build_id": BUILD_ID,
            "repo_url": REPO_URL,
            "upgrade_cmd": _upgrade_hint(),
            "mpb_update": mpb_update_info(),
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
            elif path == "/icon.svg":
                self._send(200, ICON_SVG, "image/svg+xml")
            elif path == "/api/data":
                payload = read_palace(force=force)
                payload["version"] = version_info()
                self._send(200, json.dumps(payload), "application/json")
            elif path == "/api/revisions":
                did = urllib.parse.parse_qs(qs).get("id", [""])[0]
                if not did:
                    self._send(400, json.dumps({"error": "id required"}), "application/json")
                else:
                    self._send(200, json.dumps(revisions_for(did)), "application/json")
            elif path == "/api/galaxy":
                self._send(200, json.dumps(galaxy_info()), "application/json")
            elif path == "/api/version":
                self._send(200, json.dumps(version_info(force=True)), "application/json")
            elif path == "/api/update":
                self._send(200, json.dumps(mpb_update_info(force=True)), "application/json")
            elif path == "/api/health":
                # Reports the truth, including failure. Never a blanket "ok".
                #
                # `ok` answers one narrow question — is the palace readable —
                # so it stays a clean 200/503 for anything scripting this.
                # `status` is the honest overall state, and it degrades to
                # "attention" while any warning is live. A green light with an
                # active warning underneath it is how you end up trusting a
                # health check that cannot fail.
                #
                # Scope: this endpoint reports PALACE INTEGRITY — can we read
                # it, and is anything about the stored data wrong. The UI adds
                # operational context on top (disk, version, chunk ordering).
                # The two must never disagree about green: anything that makes
                # the chip amber for an integrity reason belongs here too.
                try:
                    d = read_palace()
                    notes = list(d.get("palace_notes") or [])
                    kg = (d.get("layers") or {}).get("kg") or {}
                    if kg.get("rival"):
                        notes.append(
                            "two knowledge graphs: this reads %s (%d entities, %d triples); "
                            "an MCP server started with --palace reads %s instead"
                            % (kg.get("path"), kg.get("entities", 0), kg.get("triples", 0),
                               kg["rival"])
                        )
                    ig = (d.get("ops") or {}).get("integrity") or {}
                    if ig.get("checked") and ig.get("errors"):
                        notes.append("SQLite integrity check failed: "
                                     + "; ".join(ig["errors"][:3]))
                    self._send(200, json.dumps({
                        "ok": True,
                        "status": "attention" if notes else "healthy",
                        "drawers": d["count"],
                        "palace": PALACE_PATH,
                        "warnings": notes,
                        "integrity": {"checked": bool(ig.get("checked")),
                                      "errors": len(ig.get("errors") or [])},
                    }), "application/json")
                except Exception as e:
                    self._send(503, json.dumps(
                        {"ok": False, "status": "error", "error": str(e)}
                    ), "application/json")
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
<link rel="icon" type="image/svg+xml" href="/icon.svg">
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

  /* nowrap, deliberately: with a flex:1 search field in here, wrapping drops
     the clock onto a second row and shoves the whole grid down the page. The
     field shrinks instead (min-width:0 below), and the pieces that stop
     earning their width get dropped by the media queries. */
  header { display:flex; align-items:center; gap:16px; flex-wrap:nowrap;
           padding:0 22px; height:64px; background:rgb(5 8 20 / .95);
           border-bottom:1px solid var(--line); backdrop-filter:blur(6px); }
  header .mark { width:26px; height:26px; flex:none; }
  header .chip, header .brand, header .clock, header button.refresh { flex:none; }
  @media (max-width:1180px){ .tagline { display:none; } }
  @media (max-width:1020px){ .clock { display:none; } }
  @media (max-width:880px){ #cntChip, #verChip .who { display:none; } }
  @media (max-width:820px){
    header { flex-wrap:wrap; height:auto; padding:9px 14px; gap:10px; }
    #q { order:10; flex:1 0 100%; }
  }
  .brand { margin-right:6px; }
  .wordmark { font-size:1.45rem; font-weight:700; letter-spacing:-.025em;
              white-space:nowrap; line-height:1.1; }
  .wordmark .b { color:var(--brand); }
  /* Says the two things a first-time viewer cannot otherwise learn: what this
     is for, and that looking at it changes nothing. The palace path used to
     live here; it is a local username and directory layout, it is in every
     screenshot anyone ever posts, and it belongs in System status instead. */
  .tagline { font-size:10.5px; color:var(--fg-subtle); white-space:nowrap; }
  .tagline b { color:var(--fg-muted); font-weight:600; }

  .chip { font-size:11.5px; padding:3px 9px; border-radius:999px; white-space:nowrap;
          border:1px solid var(--line-2); background:var(--surface); color:var(--fg-muted); }
  .chip b { color:var(--fg); font-weight:600; }
  .chip .who { color:var(--graphite); font-weight:500; }
  .chip.act { cursor:pointer; }
  .chip.act:hover { border-color:var(--brand); }
  .chip.current { border-color:rgb(34 197 94 / .5); color:var(--success); }
  .chip.update  { border-color:rgb(245 158 11 / .55); color:var(--warning); }
  .chip.unknown { border-color:rgb(239 68 68 / .5); color:var(--error); }
  .chip.new     { border-color:var(--brand); color:var(--brand); }
  .chip.ok      { border-color:rgb(34 197 94 / .5); color:var(--success); }
  .chip.warn    { border-color:rgb(245 158 11 / .55); color:var(--warning); }
  .chip.bad     { border-color:rgb(239 68 68 / .5); color:var(--error); }

  /* Drawers vs Documents. Two lenses over the same 246 drawers, never two
     hierarchies — a document's chunks scatter across rooms, so it cannot be a
     branch of the tree. */
  .views { display:flex; gap:6px; }
  .view { flex:1; cursor:pointer; font:inherit; font-size:11.5px; padding:5px 6px;
          border-radius:6px; border:1px solid var(--line-2); background:var(--surface);
          color:var(--fg-muted); }
  .view:hover { border-color:var(--brand); color:var(--fg); }
  .view.sel { border-color:var(--brand); color:var(--fg); background:var(--surface-raised); }
  .view b { color:var(--fg); font-variant-numeric:tabular-nums; }
  .view:focus-visible { outline:none; box-shadow:var(--shadow-brand); }

  /* Overlay sheets: System status and the vocabulary intro. */
  #overlay { position:fixed; inset:0; background:rgb(2 6 13 / .72); z-index:50;
             display:grid; place-items:center; padding:24px; }
  #overlay[hidden] { display:none; }
  .sheet { max-width:680px; width:100%; max-height:82vh; overflow-y:auto;
           background:var(--surface); border:1px solid var(--line-2);
           border-radius:11px; padding:22px 24px; }
  .sheet h2 { font-size:15px; margin:0 0 4px; }
  .sheet .sub { font-size:12px; color:var(--fg-subtle); margin:0 0 16px; }
  .sheet h3 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
              color:var(--fg-subtle); margin:18px 0 8px; font-weight:600; }
  .sheet dl.kv { display:grid; grid-template-columns:auto 1fr; gap:5px 14px;
                 margin:0; font-size:12.5px; }
  .sheet dl.kv dt { color:var(--fg-subtle); }
  .sheet dl.kv dd { margin:0; color:var(--fg-muted); font-family:var(--mono);
                    font-size:11.5px; word-break:break-all; }
  .sheet .close { float:right; cursor:pointer; border:1px solid var(--line-2);
                  background:var(--surface-raised); color:var(--fg-muted);
                  border-radius:6px; font:inherit; font-size:12px; padding:3px 9px; }
  .sheet .close:hover { border-color:var(--brand); color:var(--fg); }
  /* Advisories: a warning plus something you can act on. */
  .adv { border:1px solid var(--line-2); border-left:2px solid var(--warning);
         border-radius:0 8px 8px 0; padding:11px 13px; margin:8px 0;
         background:rgb(245 158 11 / .05); }
  .adv.bad { border-left-color:var(--error); background:rgb(239 68 68 / .06); }
  .adv.info { border-left-color:var(--brand); background:transparent; }
  /* Galaxy view */
  #content.gx { padding:0; overflow:hidden; display:flex; flex-direction:column; }
  .galaxy { display:flex; flex-direction:column; height:100%; min-height:0; }
  .gx-bar { display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; padding:9px 14px;
            border-bottom:1px solid var(--line); font-size:12px; color:var(--fg-muted); background:var(--bg-alt); }
  .gx-bar label { display:flex; align-items:center; gap:6px; cursor:pointer; }
  .gx-bar select, .gx-bar button { font:inherit; font-size:12px; background:var(--surface-raised);
            color:var(--fg-muted); border:1px solid var(--line-2); border-radius:6px; padding:3px 8px; cursor:pointer; }
  .gx-bar button:hover { border-color:var(--brand); color:var(--fg); }
  .gx-bar input[type=range] { width:190px; accent-color:var(--brand); }
  .gx-date { font-family:var(--mono); min-width:80px; color:var(--fg); }
  .gx-wrap { position:relative; flex:1; min-height:0; background:radial-gradient(ellipse at center, #0b1330 0%, var(--bg) 72%); }
  #gxc { position:absolute; inset:0; width:100%; height:100%; display:block; cursor:grab; }
  #gxc.drag { cursor:grabbing; }
  .gx-legend { position:absolute; top:10px; left:12px; font-size:11px; max-height:72%; overflow:auto;
               background:rgb(5 8 20 / .55); border:1px solid var(--line); border-radius:8px; padding:6px 9px; }
  .gx-legend div { cursor:pointer; padding:1px 0; color:var(--fg-muted); white-space:nowrap; }
  .gx-legend div:hover { color:var(--fg); }
  .gx-legend i { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:-1px; }
  .gx-legend .off { opacity:.3; }
  .gx-key { position:absolute; bottom:26px; left:12px; font-size:11px; color:var(--fg-muted); pointer-events:none;
            background:rgb(5 8 20 / .55); border:1px solid var(--line); border-radius:8px; padding:5px 9px; }
  .gx-key i.rg { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:7px; vertical-align:-1px;
                 border:1.5px solid #fff; box-sizing:border-box; }
  .gx-compact .gx-key { font-size:10px; bottom:20px; }
  .gx-chain { position:absolute; top:10px; right:12px; width:250px; max-height:68%; overflow:auto; font-size:11px; color:var(--fg-muted);
              background:rgb(5 8 20 / .78); border:1px solid #22e0d0; border-radius:8px; padding:6px 9px; }
  .gx-chain .ch-h { color:#22e0d0; font-weight:600; margin-bottom:4px; }
  .gx-chain .ch-n { color:var(--fg-subtle); }
  .gx-chain .ch-e { color:#22e0d0; font-size:10.5px; margin:3px 0 3px 8px; }
  .gx-chain .ch-r { padding:3px 5px; border-radius:5px; cursor:pointer; }
  .gx-chain .ch-r:hover { background:rgb(34 224 208 / .12); }
  .gx-chain .ch-r.me { border-left:3px solid #ff3df0; }
  .gx-compact .gx-chain { width:210px; font-size:10px; }
  .gx-tip { position:absolute; pointer-events:none; max-width:340px; background:var(--surface-raised);
            border:1px solid var(--line-2); border-radius:8px; padding:8px 10px; font-size:11.5px;
            color:var(--fg-muted); z-index:5; }
  .gx-tip b { color:var(--fg); }
  .gx-msg { position:absolute; left:50%; top:48%; transform:translate(-50%,-50%); text-align:center;
            color:var(--fg-muted); font-size:13px; pointer-events:none; max-width:70%; }
  .gx-hint { position:absolute; right:12px; bottom:9px; font-size:10.5px; color:var(--fg-disabled); }
  /* Main page: latest entries on the left, the Galaxy on the right */
  #content.split { padding:0; overflow:hidden; display:grid; grid-template-columns:minmax(340px,42%) minmax(0,1fr); }
  #listPane { overflow-y:auto; padding:16px 20px; min-width:0; }
  #gxPane { min-width:0; min-height:0; display:flex; flex-direction:column; border-left:1px solid var(--line); }
  #gxPane .galaxy { flex:1; }
  .gx-compact .gx-opt { display:none; }
  /* entries expand in place */
  .card .chev { color:var(--fg-subtle); font-size:11px; margin-right:6px; display:inline-block; width:10px; }
  .card.open { border-color:var(--line-2); }
  .card:focus-visible { outline:none; box-shadow:var(--shadow-brand); }
  .card .meta { font-size:11px; color:var(--fg-subtle); margin:8px 0 6px; }
  .card .full { white-space:pre-wrap; word-break:break-word; font-family:var(--mono); font-size:12px; line-height:1.5;
                max-height:520px; overflow:auto; margin:0; padding:10px 12px; background:var(--bg-alt);
                border:1px solid var(--line); border-radius:6px; color:var(--fg-muted); cursor:text; }
  .card .acts { display:flex; flex-wrap:wrap; gap:8px; margin-top:9px; }
  .card .acts button { font:inherit; font-size:11.5px; background:var(--surface-raised); color:var(--fg-muted);
                       border:1px solid var(--line-2); border-radius:6px; padding:3px 9px; cursor:pointer; }
  .card .acts button:hover { border-color:var(--brand); color:var(--fg); }
  .crumb .xa a { color:var(--fg-subtle); cursor:pointer; }
  .crumb .xa a:hover { color:var(--brand); }
  .revbox { margin-top:18px; border-top:1px solid var(--line); padding-top:12px; }
  .rv-h { font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:var(--fg-subtle); margin-bottom:8px; }
  .rv { font-size:12px; padding:7px 10px; margin:6px 0; border-left:2px solid var(--line-2); background:var(--surface); border-radius:0 6px 6px 0; }
  .rv.cur { border-left-color:var(--success); }
  .rv.old { opacity:.78; }
  .cf { padding:8px 0; border-top:1px solid var(--line); font-size:12.5px; }
  .cf-go { color:var(--brand); cursor:pointer; font-size:11.5px; }
  .rv-more { display:inline-block; margin-top:3px; font-size:11px; color:var(--brand); cursor:pointer; }
  .rv-p { margin-top:4px; color:var(--fg-subtle); white-space:pre-wrap; word-break:break-word; font-size:11.5px; }
  .rv-n { font-size:11px; color:var(--fg-disabled); margin-top:8px; }
  .gx-compact .gx-hint { display:none; }
  .gx-compact .gx-legend { max-height:46%; font-size:10px; }
  .galaxy:not(.gx-compact) .gx-co { display:none; }
  @media (max-width:1180px){ #content.split { grid-template-columns:1fr; } #gxPane { display:none; } }
  .adv-t { font-size:12.5px; font-weight:600; color:var(--fg); margin-bottom:5px; }
  .adv-w { font-size:12px; color:var(--fg-muted); line-height:1.55; }
  .fx { margin-top:9px; }
  .fx-h { font-size:11px; font-weight:600; color:var(--brand); margin-bottom:4px; }
  .fx-c { background:var(--bg); border:1px solid var(--line); border-radius:6px;
          padding:8px 10px; margin:0; font-family:var(--mono); font-size:11px;
          line-height:1.55; color:var(--fg-muted); white-space:pre-wrap;
          word-break:break-word; overflow-x:auto; }
  .fx-n { font-size:11px; color:var(--fg-subtle); margin-top:4px; line-height:1.5; }
  .fx-u { font-size:11px; color:var(--warning); margin-top:9px; line-height:1.5;
          border-top:1px dashed var(--line-2); padding-top:8px; }
  .fx-m { margin-top:9px; font-size:11px; color:var(--fg-subtle); }
  .fx-m b { color:var(--fg-muted); display:block; margin-bottom:3px; font-size:11px; }
  .fx-b { margin-top:10px; cursor:pointer; font:inherit; font-size:11px; padding:4px 10px;
          border-radius:6px; border:1px solid var(--line-2); background:var(--surface-raised);
          color:var(--fg-muted); }
  .fx-b:hover { border-color:var(--brand); color:var(--fg); }
  .fx-b:focus-visible { outline:none; box-shadow:var(--shadow-brand); }

  .note { border-left:2px solid var(--warning); background:rgb(245 158 11 / .07);
          padding:9px 12px; margin:8px 0; font-size:12.5px; color:var(--fg-muted);
          border-radius:0 6px 6px 0; }
  .note.bad { border-left-color:var(--error); background:rgb(239 68 68 / .07); }
  .vocab { font-size:13px; color:var(--fg-muted); }
  .vocab p { margin:0 0 10px; }
  .vocab b { color:var(--fg); }

  /* Layer coverage. The bar is the argument: it shows what the palace
     advertises next to what is actually in it. */
  .lay { display:grid; grid-template-columns:82px 1fr auto; gap:4px 8px;
         align-items:center; font-size:11.5px; }
  .lay .k { color:var(--fg-muted); }
  .lay .track { height:5px; border-radius:3px; background:var(--surface-raised);
                overflow:hidden; }
  .lay .track i { display:block; height:100%; background:var(--brand); }
  .lay .track i.zero { background:var(--fg-disabled); }
  .lay .v { color:var(--fg-subtle); font-variant-numeric:tabular-nums; }
  .lay .v.none { color:var(--fg-disabled); }

  /* About: whose program this is, which version, and where the source lives.
     Foot of the informational column — it is a fact about the deployment, not
     a place to navigate to. The disclaimer sits here rather than buried in a
     dialog because "MemPalace Browser" reads like a first-party product and
     it is not one. */
  /* margin-top:auto eats the slack in the flex column, pinning this to the
     bottom when the panel is short and letting it flow normally when the
     content is tall enough to scroll. */
  .about { border-top:1px solid var(--line); margin-top:auto; padding-top:11px;
           display:flex; gap:9px; align-items:flex-start; flex:none; }
  .about .ic { width:20px; height:20px; flex:none; opacity:.85; }
  .about .nm { font-size:11.5px; font-weight:600; color:var(--fg-muted); line-height:1.35; }
  .about .nm b { color:var(--brand); font-weight:600; font-variant-numeric:tabular-nums; }
  /* The byline. Graphite is the logo's middle layer, so it sits under the
     product name without competing with it — and goes brand blue on hover to
     admit it is a link. */
  .about .bld { font-weight:500; color:var(--fg-subtle); font-family:var(--mono);
                font-size:9.5px; letter-spacing:0; }
  .about .by { display:block; font-size:10px; color:var(--graphite);
               text-decoration:none; letter-spacing:.015em; margin-top:1px; }
  .about .by:hover { color:var(--brand); }
  .about .sub { font-size:10.5px; color:var(--fg-subtle); line-height:1.45; margin-top:2px; }
  .about a { color:var(--brand); text-decoration:none; }
  .about a:hover { color:var(--brand-hover); text-decoration:underline; }

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

  .clock { text-align:right; font-variant-numeric:tabular-nums; line-height:1.3; }
  .clock .local { font-size:16px; font-weight:600; letter-spacing:-.01em; }
  .clock .utc { font-size:11px; color:var(--fg-subtle); font-family:var(--mono); }

  /* Three columns, and the split is a rule not a habit: LEFT navigates (where
     do I go), CENTRE is content, RIGHT informs (what is true about this
     palace). Nothing on the right is clickable navigation, nothing on the left
     is a statistic. */
  main { display:grid; grid-template-columns:272px 1fr 272px; height:calc(100vh - 64px); }
  #side { border-right:1px solid var(--line); background:var(--bg-alt);
          display:grid; grid-template-rows:auto 1fr; min-height:0; }
  #sideTop { padding:11px; border-bottom:1px solid var(--line); }
  #tree { overflow-y:auto; padding:8px 11px; min-height:0; }
  /* Column, so About can be pushed to the foot with margin-top:auto. It is a
     colophon — true of the program rather than of your palace — so it belongs
     out of the way of the figures, not stacked under them like another stat. */
  #info { border-left:1px solid var(--line); background:var(--bg-alt);
          overflow-y:auto; padding:11px; min-height:0;
          display:flex; flex-direction:column; }
  @media (max-width:1180px){
    main { grid-template-columns:272px 1fr; }
    #info { display:none; }          /* content beats chrome when space is tight */
  }
  @media (max-width:820px){
    main { grid-template-columns:1fr; height:auto; }
    #side { grid-template-rows:none; } #tree { max-height:40vh; }
    #info { display:block; border-left:none; border-top:1px solid var(--line); }
  }

  /* Search lives in the header, filling the gap between the chips and Refresh.
     It belongs with the global controls rather than above the tree, because it
     is global: matchesQuery() deliberately ignores the selected wing so the
     tree can show where hits live across the whole palace. Sitting above the
     tree implied it searched the selection. It never did. */
  #q { flex:1 1 auto; min-width:0; padding:8px 11px; border-radius:7px;
       border:1px solid var(--line-2);
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
  details.sb { border-top:1px solid var(--line); padding:5px 0; }
  details.sb > summary { list-style:none; cursor:pointer; display:flex; align-items:baseline; gap:6px; padding:2px 0; }
  details.sb > summary::-webkit-details-marker { display:none; }
  details.sb > summary::before { content:"▸"; color:var(--fg-subtle); font-size:9px; width:9px; flex:none; }
  details.sb[open] > summary::before { content:"▾"; }
  details.sb > summary .st-h { margin:0; flex:none; }
  details.sb > summary:hover .st-h { color:var(--fg-muted); }
  .sb-sum { margin-left:auto; font-size:10.5px; font-family:var(--mono); color:var(--fg-muted); text-align:right; min-width:0;
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sb-sum.sev-warn { color:var(--warning); } .sb-sum.sev-bad { color:var(--error); }
  details.sb[open] > summary .sb-sum { opacity:.7; }
  .sb-body { padding:7px 0 3px 15px; }
  .sb-about { margin-top:auto; padding-top:10px; border-top:1px solid var(--line); }
  .sb-tools { font-size:10px; color:var(--fg-subtle); text-align:right; margin-bottom:3px; }
  .sb-tools a { cursor:pointer; text-decoration:underline; text-underline-offset:2px; }
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
  .pill.sec { background:var(--warning); color:#1a1a1a; }
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

  /* A reassembled document. Chunk seams stay visible: this is 27 verbatim
     slices shown in order, not a file we recovered — and each seam is where
     the miner made a filing decision worth being able to see. */
  .card .docmeta { font-size:11px; color:var(--fg-subtle); }
  .card .nchunk { font-size:9px; font-weight:700; letter-spacing:.06em; padding:1px 5px;
                  border-radius:3px; background:var(--surface-raised);
                  color:var(--fg-muted); border:1px solid var(--line-2); }
  .seam { display:flex; align-items:center; gap:9px; margin:0; padding:7px 0 6px;
          font-size:10.5px; color:var(--fg-subtle); font-family:var(--mono); }
  .seam::after { content:""; flex:1; height:1px; background:var(--line); }
  .seam .rm { color:var(--brand); }
  .chunks { background:var(--surface); border:1px solid var(--line); border-radius:8px;
            padding:6px 14px 14px; }
  .chunks pre { white-space:pre-wrap; word-wrap:break-word; font-size:12.5px;
                font-family:var(--mono); line-height:1.65; margin:0;
                color:var(--fg-muted); }
  .partof { font-size:12px; color:var(--fg-muted); background:var(--surface);
            border:1px solid var(--line); border-left:2px solid var(--brand);
            border-radius:0 7px 7px 0; padding:8px 12px; margin:0 0 12px; }
  .partof a { color:var(--brand); cursor:pointer; text-decoration:none; }
  .partof a:hover { color:var(--brand-hover); }
</style>
</head>
<body>
<header>
  <img class="mark" src="/icon.svg" alt="">
  <div class="brand">
    <div class="wordmark">MemPalace<span class="b"> Browser</span></div>
    <div class="tagline">Browse and search your palace · <b>Read-only</b></div>
  </div>
  <span class="chip act" id="healthChip" title="System status">status…</span>
  <span class="chip act" id="verChip" title="Click to re-check PyPI">version…</span>
  <span class="chip" id="cntChip">…</span>
  <span class="chip new" id="newChip" style="display:none"></span>
  <span class="chip act" id="helpChip" title="What are wings and drawers?">?</span>
  <input id="q" placeholder="Search drawer text across the whole palace…" autocomplete="off">
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
    <div id="sideTop">
      <div class="views">
        <button class="view sel" data-view="drawers">Drawers <b id="vnDrawers">—</b></button>
        <button class="view" data-view="documents">Documents <b id="vnDocs">—</b></button>
        <button class="view" data-view="galaxy">Galaxy <b>✦</b></button>
      </div>
    </div>
    <div id="tree"></div>
  </div>
  <div id="content"><div class="empty">Loading palace…</div></div>
  <aside id="info"></aside>
</main>
<div id="overlay" hidden><div class="sheet" id="sheet"></div></div>
<script>
let DATA = null, sel = {wing:null, room:null}, query = "", lastSeen = null, readAt = null;
// "drawers" = the wing/room tree. "documents" = chunks regrouped into the file
// they were mined from. Two lenses over one set of drawers, never two trees.
let view = "drawers";
let byId = {};              // drawer id -> drawer, for document assembly
let docOfDrawer = {};       // drawer id -> the document it belongs to

const LS_KEY = "mpb.lastSeen";

// ---- clock: rendered in YOUR browser's timezone. The box runs UTC, so
// server-side rendering would show the wrong time. This is always right.
function tick(){
  const now = new Date();
  // No seconds. A ticking second-hand is motion where nothing is happening —
  // it draws the eye away from content that changes only when the palace does.
  document.getElementById("clkLocal").textContent =
    now.toLocaleString(undefined, {weekday:"short", month:"short", day:"numeric",
                                   hour:"2-digit", minute:"2-digit"});
  document.getElementById("clkUtc").textContent =
    Intl.DateTimeFormat().resolvedOptions().timeZone + " · "
    + now.toISOString().slice(0,16).replace("T"," ") + "Z";
  if(readAt) document.getElementById("ago").textContent = "· " + ago(readAt);
}
setInterval(tick, 15000); tick();   // minute resolution needs no 1s timer

function ago(iso){
  const s = Math.max(0, (Date.now() - new Date(iso).getTime())/1000);
  if(s < 60) return "just now";
  if(s < 3600) return Math.floor(s/60) + "m ago";
  if(s < 86400) return Math.floor(s/3600) + "h ago";
  return Math.floor(s/86400) + "d ago";
}
const esc = s => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
// Credential-shaped identifiers are masked wherever a drawer is SUMMARISED (previews, tooltips, chain and plan titles).
// The full text, opened on purpose, is shown as filed. UUID-style ids are left alone: they are not credentials.
const PV_RX = [
  [/\b([\w.-]+@[a-z][a-z0-9]*!)[\w-]{2,}/g, "$1•••"],                                   // Proxmox token id: keep user@realm!, hide the token name
  [/\b\d{8,12}(?=:(?:\[REDACTED\]|\.{2,}|…|[A-Za-z0-9_-]{20,}))/g, "••••••••"],
  [/\b(?:token|bot)\b([^\n]{0,24}?)\b\d{8,12}\b(?=:)/gi, (m, g) => m.replace(/\d{8,12}/, "••••••••")],
  [/\b(allowed[ _-]?users?\b[^\n]{0,24}?\bID:?[ \t]*)\d{6,12}\b/gi, "$1••••••••"],                   // numeric half of a chat-bot token
  [/\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g, "•••(key)"], [/\bgh[pousr]_[A-Za-z0-9]{20,}\b/g, "•••(key)"], [/\bxox[baprs]-[A-Za-z0-9-]{10,}\b/g, "•••(key)"],
  [/\bsk-[A-Za-z0-9_-]{20,}\b/g, "•••(key)"], [/\bAIza[0-9A-Za-z_-]{30,}\b/g, "•••(key)"], [/\bntn_[A-Za-z0-9]{20,}\b/g, "•••(key)"],
  [/\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g, "•••(jwt)"], [/\b(Bearer[ \t]+)[A-Za-z0-9._~+\/=-]{24,}/gi, "$1•••"]];
function pv(s){ let t = String(s == null ? "" : s); for(const [rx, to] of PV_RX) t = t.replace(rx, to); return t; }
async function copyText(text){                       // navigator.clipboard needs https; over http on a LAN name fall back to a hidden textarea
  try{ if(navigator.clipboard && window.isSecureContext){ await navigator.clipboard.writeText(text); return true; } }catch(e){}
  try{ const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;left:-9999px;top:0";
    document.body.appendChild(ta); ta.select(); const ok = document.execCommand("copy"); ta.remove(); return !!ok; }catch(e){ return false; }
}
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
      // Picking a place in the tree is a request for drawers. Documents span
      // rooms, so a room filter cannot mean anything to them. (The Galaxy just filters.)
      if(view !== "galaxy") view = "drawers";
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
      if(view !== "galaxy") view = "drawers";
      render();
    };
    div.querySelectorAll(".room").forEach(el => el.onclick = e => {
      e.stopPropagation();
      sel = {wing:el.dataset.w, room:el.dataset.r};
      if(view !== "galaxy") view = "drawers";
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
    const k = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");   // local, not toISOString (UTC shifts the month)
    keys.push(k); counts[k] = 0;
  }
  for(const d of DATA.drawers){
    const k = (d.filed_at || "").slice(0,7);
    if(k in counts) counts[k]++;
  }
  const max = Math.max(1, ...keys.map(k => counts[k]));
  const mlab = k => new Date(+k.slice(0,4), +k.slice(5,7) - 1, 1).toLocaleDateString(undefined,{month:"short"});

  // Host first: "is this machine healthy" frames everything under it. Storage
  // only carries a top gap when something sits above it.
  const resHtml = resourcesHtml();
  const sparkHtml = `<div class="st-h">Drawers filed · 12mo</div>
    <div class="spark">${keys.map(k =>
      `<i class="${counts[k]?"has":""}" style="height:${counts[k]? Math.max(8,counts[k]/max*100):2}%"
          title="${mlab(k)} ${k.slice(0,4)}: ${counts[k]}"></i>`).join("")}</div>
    <div class="spark-x"><span>${mlab(keys[0])}</span><span>${mlab(keys[11])}</span></div>`;
  const filed12 = keys.reduce((s, k) => s + counts[k], 0);
  const infoEl = document.getElementById("info");
  infoEl.innerHTML = sbTools()
    + sbSec("service", serviceHtml(), sbSumService())
    + sbSec("trust", trustHtml(), sbSumTrust())
    + sbSec("org", auditHtml(), sbSumAudit())
    + sbSec("host", resHtml, sbSumHost())
    + sbSec("storage", storeHtml, null)
    + sbSec("filed", sparkHtml, {html: esc(filed12.toLocaleString() + " in 12 mo"), sev: "ok"})
    + sbSec("layers", `<div class="st-h">Layers</div>` + layersHtml(), {html: esc((DATA.count || 0).toLocaleString() + " drawers"), sev: "ok"})
    + `<div class="sb-about">${aboutHtml()}</div>`;
  sbWire(infoEl);
}

// MemPalace's own repository, hardcoded on purpose. Every other URL here is
// ours to change; this one is a claim about someone else's project, and
// MemPalace's README carries a malware warning about impostor domains passing
// themselves off as it. A configurable "upstream" link is a configurable way
// to send our users somewhere that is not upstream. This is the address their
// README names as official.
const MP_REPO = "https://github.com/MemPalace/mempalace";

// ---- operations: "can I trust what I am looking at?" ---------------------------
// Everything here comes from DATA.ops, which the server only ever READS.
function dur(sec){
  sec = Math.max(0, sec);
  if(sec < 90) return Math.round(sec) + " s";
  if(sec < 5400) return Math.round(sec/60) + " min";
  if(sec < 172800) return Math.round(sec/3600) + " h";
  return Math.round(sec/86400) + " days";
}
// ---- folding sidebar: every section is one line (title + its status) and opens in place. A section with a problem
// opens itself unless the user has chosen otherwise; the choice is remembered in this browser.
const SB_KEY = "mpb.sb";
function sbState(){ try{ return JSON.parse(localStorage.getItem(SB_KEY) || "{}") || {}; }catch(e){ return {}; } }
function sbSave(st){ try{ localStorage.setItem(SB_KEY, JSON.stringify(st)); }catch(e){} }
function sbSec(key, html, sum){
  if(!html) return "";
  const m = html.match(/^\s*<div class="st-h"[^>]*>([\s\S]*?)<\/div>/);
  const title = m ? m[1] : key.charAt(0).toUpperCase() + key.slice(1), body = m ? html.slice(m[0].length) : html;
  if(!sum){                                          // no custom summary: the first row's label and value
    const r = body.match(/<span>([^<]*)<\/span><span[^>]*>([^<]*)<\/span>/);
    sum = {html: r ? esc(r[1] + " " + r[2].trim()) : "", sev: "ok"};
  }
  const st = sbState(), open = (key in st) ? !!st[key] : (key === "trust" || sum.sev === "warn" || sum.sev === "bad");
  return `<details class="sb" data-k="${key}"${open ? " open" : ""}><summary><span class="st-h">${title}</span>`
    + `<span class="sb-sum sev-${sum.sev || "ok"}" title="${sum.html}">${sum.html}</span></summary><div class="sb-body">${body}</div></details>`;
}
function sbTools(){ return `<div class="sb-tools"><a data-sbx="1">expand all</a> · <a data-sbx="0">collapse all</a></div>`; }
function sbWire(root){
  root.querySelectorAll("details.sb").forEach(d => d.addEventListener("toggle", () => { const st = sbState(); st[d.dataset.k] = d.open; sbSave(st); }));
  root.querySelectorAll("[data-sbx]").forEach(a => a.onclick = () => {
    const st = sbState(), want = a.dataset.sbx === "1";
    root.querySelectorAll("details.sb").forEach(d => { d.open = want; st[d.dataset.k] = want; }); sbSave(st);
  });
  const au = root.querySelector("[data-audit]"); if(au) au.onclick = e => { e.preventDefault(); auditSheet(); };
  const rf = root.querySelector("[data-refile]"); if(rf) rf.onclick = e => { e.preventDefault(); auditOpen(); };
}
function sbSumService(){
  const o = DATA.ops || {}, id = o.identity || {}, u = DATA.mpb_update || {}; let sev = "ok", t = "up " + dur((o.now || 0) - (id.started || 0));
  if(!id.service){ t = "started by hand"; sev = "warn"; }
  const word = {none: "no release yet", current: "up to date", ahead: "ahead of release", unknown: "update check failed", disabled: "update check off", checking: "checking for updates"};
  if(u.status === "update-available"){ t += " · " + (u.latest || "") + " available"; if(sev === "ok") sev = "warn"; }
  else if(word[u.status]){ t += " · " + word[u.status]; if(u.status === "unknown" && sev === "ok") sev = "warn"; }
  return {html: esc(t), sev};
}
function sbSumTrust(){
  const o = DATA.ops || {}, ig = o.integrity || {}, bk = o.backups || {}, sc = DATA.secrets || {}, rs = DATA.revsecrets || {}, now = o.now || 0;
  const parts = []; let sev = "ok"; const bump = s => { if(s === "bad" || (s === "warn" && sev === "ok")) sev = s; };
  if(ig.checked && !(ig.errors || []).length) parts.push("ok"); else if(ig.checked){ parts.push("integrity FAILED"); bump("bad"); } else parts.push("integrity n/a");
  if(bk.configured && bk.count){ const age = now - bk.newest.mtime; parts.push("backup " + dur(age)); if(age > 30 * 86400) bump("bad"); else if(age > 7 * 86400) bump("warn"); }
  else { parts.push("no backup info"); bump("warn"); }
  if(sc.enabled && sc.total){ parts.push(sc.total + " secrets?"); bump("warn"); }
  if(rs.versions){ parts.push(rs.versions + " old secret" + (rs.versions === 1 ? "" : "s")); bump("warn"); }
  return {html: esc(parts.join(" · ")), sev};
}
function sbSumAudit(){
  const a = DATA.audit || {}, s = ((a.report || {}).scores || {}), notes = ((a.report || {}).findings || []).length;
  if(!a.available) return {html: "n/a", sev: "ok"};
  const o = s.overall; return {html: esc(o + "/100 · " + notes + " note" + (notes === 1 ? "" : "s")), sev: o < 70 ? "bad" : o < 90 ? "warn" : "ok"};
}
function sbSumHost(){
  const r = DATA.resources || {}; if(!r.enabled || !r.available) return {html: "", sev: "ok"};
  const pct = r.mem_total ? r.mem_used / r.mem_total * 100 : 0, lpc = (r.load1 / (r.cpus || 1)) * 100;
  return {html: esc("load " + r.load1.toFixed(2) + " · mem " + Math.round(pct) + "%"), sev: (pct > 90 || lpc > 100) ? "bad" : (pct > 75 || lpc > 70) ? "warn" : "ok"};
}
function updateRowHtml(){
  const u = DATA.mpb_update; if(!u) return "";
  const m = {disabled:["check off", "var(--fg-subtle)"], checking:["checking…", "var(--fg-subtle)"], unknown:["unknown", "var(--warning)"], none:["no release yet", "var(--fg-subtle)"],
             current:["up to date", "var(--success)"], ahead:["ahead of release", "var(--fg-subtle)"],
             "update-available":[esc(u.latest || "") + " available", "var(--warning)"]}[u.status] || ["unknown", "var(--warning)"];
  const note = u.status === "disabled" ? "MPB_CHECK_UPDATES=0" : u.status === "none" ? "running " + esc(u.current) + " · no release is published on GitHub yet"
    : u.latest ? "running " + esc(u.current) + " · latest release " + esc(u.latest) : esc(u.error || u.current || "");
  return `<div class="st-row"><span>Updates</span><span style="color:${m[1]}">${m[0]}</span></div><div class="st-note">${note}</div>`;
}
function mpUpgradeSteps(){
  const q = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
  const pp = DATA.palace_path || "", dd = pp.replace(/\/[^\/]+\/?$/, "") || pp;
  return "# 1. back up the whole MemPalace data folder first\nB=~/mempalace-backup-$(date +%Y%m%d-%H%M%S) && mkdir \"$B\" && cp -a " + q(dd) + "/. \"$B\"/   # the trailing /. also follows a symlinked data folder\n# 2. upgrade\n"
    + (DATA.upgrade_cmd || "pip install -U mempalace")
    + "\n# 3. restart MemPalace's server and this browser (systemd user units: systemctl --user restart <unit>)\n# 4. check the result\nmempalace audit";
}
function serviceHtml(){
  const o = DATA.ops || {}, id = o.identity;
  if(!id) return "";
  const svc = id.service ? esc(id.service)
    : "<b style=\"color:var(--warning)\">started by hand</b>";
  return `<div class="st-h">This service</div>
    <div class="st-row"><span>Host</span><span>${esc(id.host)}</span></div>
    <div class="st-row"><span>Service</span><span>${svc}</span></div>
    <div class="st-row"><span>Up</span><span>${dur((o.now||0) - (id.started||0))}</span></div>
    <div class="st-row"><span>Listening</span><span>${esc(id.bind)}:${esc(String(id.port))}</span></div>
    <div class="st-note">pid ${esc(String(id.pid))}${id.user ? " · " + esc(id.user) : ""}${
      id.service ? "" : " · will not survive a reboot"}</div>` + updateRowHtml();
}
function trustHtml(){
  const o = DATA.ops || {}, ig = o.integrity || {}, bk = o.backups || {}, now = o.now || 0;
  let igRow;
  if(!ig.available){
    igRow = `<div class="st-row"><span>Integrity</span><span style="color:var(--fg-subtle)">n/a</span></div>
      <div class="st-note">${esc(ig.reason || "not available")}</div>`;
  } else if(!ig.checked){
    igRow = `<div class="st-row"><span>Integrity</span><span style="color:var(--warning)">not checked</span></div>
      <div class="st-note">${esc(ig.reason || "no database to check")}</div>`;
  } else if((ig.errors || []).length){
    igRow = `<div class="st-row"><span>Integrity</span><span style="color:var(--error)">FAILED</span></div>
      <div class="st-note">${esc(ig.errors[0])}</div>`;
  } else {
    igRow = `<div class="st-row"><span>Integrity</span><span style="color:var(--success)">ok</span></div>
      <div class="st-note">SQLite quick check · ${dur(now - ig.checked_at)} ago</div>`;
  }
  let bkRow;
  if(!bk.configured){
    bkRow = `<div class="st-row"><span>Last backup</span><span style="color:var(--fg-subtle)">unknown</span></div>
      <div class="st-note">Backup location not set. Set MPB_BACKUP_GLOBS to see how fresh it is.</div>`;
  } else if(!bk.count){
    bkRow = `<div class="st-row"><span>Last backup</span><span style="color:var(--error)">none found</span></div>
      <div class="st-note">Nothing matches MPB_BACKUP_GLOBS.</div>`;
  } else {
    const age = now - bk.newest.mtime;
    const c = age > 30*86400 ? "var(--error)" : age > 7*86400 ? "var(--warning)" : "var(--success)";
    bkRow = `<div class="st-row"><span>Last backup</span><span style="color:${c}">${dur(age)} ago</span></div>
      <div class="st-note" title="${esc(bk.newest.name)}">${esc(bk.newest.name)} · ${bytes(bk.newest.bytes)} · ${bk.count} found</div>`;
  }
  const sc = DATA.secrets || {};
  let scRow = "";
  if(sc.enabled === false){
    scRow = `<div class="st-row"><span>Secrets</span><span style="color:var(--fg-subtle)">scan off</span></div>
      <div class="st-note">MPB_SECRET_SCAN=0</div>`;
  } else if(sc.enabled && !sc.total){
    scRow = `<div class="st-row"><span>Secrets</span><span style="color:var(--success)">none found</span></div>
      <div class="st-note">pattern scan · ${esc(String(sc.scanned))} drawers${sc.allowed ? " · " + sc.allowed + " allowed" : ""}</div>`;
  } else if(sc.enabled){
    scRow = `<div class="st-row"><span>Secrets</span><span style="color:var(--warning)">${esc(String(sc.total))} possible</span></div>
      <div class="st-note">${esc(String(sc.drawers))} drawer${sc.drawers === 1 ? "" : "s"} · masked · see warnings</div>`;
  }
  const rs = DATA.revsecrets || {};
  if(rs.versions) scRow += `<div class="st-note" style="color:var(--warning)">${esc(String(rs.versions))} old version${rs.versions === 1 ? "" : "s"} in the revision log · see warnings</div>`;
  return `<div class="st-h">Trust</div>` + igRow + bkRow + scRow;
}

// ---- audit: "how well organised is the palace?" ---------------------------------
function scoreColor(n){ return n == null ? "var(--fg-subtle)" : n >= 80 ? "var(--success)" : n >= 50 ? "var(--warning)" : "var(--error)"; }
function auditHtml(){
  const a = DATA.audit || {};
  if(!a.available){
    return `<div class="st-h">Organization</div><div class="st-note">${esc(a.reason || "audit not available")}</div>`;
  }
  const r = a.report || {}, s = r.scores || {};
  const row = (label, v) => `<div class="st-row"><span>${label}</span><span style="color:${scoreColor(v)}">${v == null ? "n/a" : esc(String(v))}</span></div>`;
  const notes = (r.findings || []).length;
  return `<div class="st-h">Organization</div>
    <div class="st-row"><span>Overall</span><span style="color:${scoreColor(s.overall)}"><b>${esc(String(s.overall))}</b> / 100</span></div>
    ${row("Rooms", s.rooms)}${row("Naming", s.naming)}${row("Tunnels", s.tunnels)}${row("Hallways", s.hallways)}${row("Knowledge graph", s.knowledge_graph)}
    <div class="st-note"><a href="#" data-audit style="color:var(--brand)">full audit · ${notes} note${notes === 1 ? "" : "s"}</a> · <a href="#" data-refile style="color:var(--brand)" title="Read-only plan for re-filing drawers">re-file plan</a></div>`;
}
// Values from the palace (wing names) go into commands as QUOTED DATA, never bare: a wing
// name is written by whatever filed the drawer, so it is untrusted input.
const q = v => JSON.stringify(String(v));
function auditIssues(){
  const r = ((DATA.audit || {}).report) || {}, out = [];
  const nm = r.naming || {}, tn = r.tunnels || {}, hl = r.hallways || {}, kg = r.knowledge_graph || {}, rm = r.rooms || {};
  for(const p of (nm.wing_prefix_pairs || [])){
    out.push({level:"info", title:`Possibly one project filed twice: ${p[0]} / ${p[1]}`,
      why:"Two wings share a prefix. The audit does not score this: it may be deliberate (for example two different "
         +"agents' notes). It is a question for you, not a fault.",
      fixes:[{label:"Look before deciding (read-only, ask your agent)",
              cmd:`mempalace_list_drawers wing=${q(p[0])} limit=5\nmempalace_list_drawers wing=${q(p[1])} limit=5`,
              note:"If they are the same project, re-file one wing's drawers with mempalace_update_drawer. "
                  +"MemPalace lowercases wing names on write, so the lowercase form is the canonical one."}],
      verify:"mempalace audit   # the pair should no longer be listed",
      undo:"Re-file the drawers back with mempalace_update_drawer."});
  }
  for(const [label, list] of [["wing spellings", nm.wing_drift], ["room spellings", nm.room_drift], ["mixed-case wings", nm.mixed_wings]]){
    if((list || []).length){
      out.push({level:"warn", title:`Colliding ${label}: ${list.length}`,
        why:"The same name is filed under more than one spelling, so counts and filters split. Search is "
           +"case-insensitive, so nothing is hidden, but the audit and status counts disagree.",
        fixes:[{label:"What the audit found", cmd: JSON.stringify(list, null, 1).slice(0, 1200),
                note:"Re-file the minority spelling with mempalace_update_drawer (wing= or room=). The tool lowercases "
                    +"wing names on write."}],
        verify:"mempalace audit", undo:"Re-file back with mempalace_update_drawer."});
    }
  }
  if((nm.tiny_wings || []).length){
    out.push({level:"info", title:`${nm.tiny_wings.length} wings hold 5 drawers or fewer`,
      why:"Small wings are not wrong, but a wing of one or two drawers is often a name that should have been a room "
         +"in a bigger wing.",
      fixes:[{label:"Which (wing: drawers)", cmd:(nm.tiny_wings || []).map(t => `${t.wing}: ${t.drawers}`).join("\n")}],
      verify:"mempalace audit", undo:"n/a — nothing here changes anything."});
  }
  if(tn.total === 0){
    out.push({level:"info", title:"No tunnels",
      why:"Tunnels link related rooms across wings so an agent can follow a topic. None exist yet.",
      fixes:[{label:"Propose tunnels from shared entities (read-only plan)", cmd:"mempalace tunnels propose",
              note:"Hand-filed drawers carry no entities, so this may find nothing; mempalace_create_tunnel links two rooms by hand."}],
      verify:"mempalace audit", undo:"mempalace_delete_tunnel removes one."});
  } else if(tn.never_traversed > 0){
    out.push({level:"info", title:`${tn.never_traversed} of ${tn.total} tunnels have never been followed`,
      why:"Not scored. A tunnel counts as followed when an agent crosses it with mempalace_follow_tunnels, so this "
         +"falls as the tunnels get used.",
      fixes:[], verify:"mempalace audit", undo:"n/a — nothing here changes anything."});
  }
  if(!hl.total){
    out.push({level:"info", title:"No hallways",
      why:"Hallways are entity links built when files are mined. Drawers filed by hand, by diary or by relay carry no "
         +"entities, so they stay at zero unless drawers are mined from source files.",
      fixes:[], verify:"mempalace audit", undo:"n/a — nothing here changes anything."});
  }
  if(kg.one_off_predicates > 0){
    out.push({level:"warn", title:`${kg.one_off_predicates} knowledge-graph predicates are used only once`,
      why:"Facts cannot be queried by relation when almost every fact has its own predicate. Sample: "
         +(kg.one_off_sample || []).slice(0, 6).join(", "),
      fixes:[{label:"Plan a clean-up (needs an LLM endpoint)", cmd:"mempalace kg normalize",
              note:"External LLM endpoints require explicit consent and may cost money. The alternative is to re-file "
                  +"the facts by hand with kg_add and kg_invalidate."}],
      verify:"mempalace audit", undo:"Ended facts keep their history; nothing is deleted."});
  }
  if((rm.flat_wings || []).length){
    out.push({level:"info", title:`${rm.flat_wings.length} wings have no room structure`,
      why:"Every drawer in these wings sits in one room, so rooms add no information.",
      fixes:[{label:"Wings", cmd:(rm.flat_wings || []).map(w => JSON.stringify(w)).join("\n")}],
      verify:"mempalace audit", undo:"n/a — nothing here changes anything."});
  }
  return out;
}
// CHAIN-CORE-BEGIN
// Supersede chains between DRAWERS (not versions of one drawer: MemPalace's revision log records those, and History shows them).
// Many drawers say in plain words that they replace an older one ("Supersedes the 2026-10-12 wrap"). That older drawer is named
// by date or title, never by id, so it is FOUND here, with reasons: an earlier drawer in the same wing that is close in meaning
// and shares the date or version the sentence names. Read-only, labelled as inferred, never a verdict.
const CH_K = 10;
const CH_CLAIM = /\b(supersed(?:es|ed|ing)|replac(?:es|ed|ing)|obsoletes?)\b/i;
const CH_DATE = /\b20\d\d-\d\d-\d\d\b/g, CH_VER = /\bv?\d+\.\d+(?:\.\d+)?\b/g;
function chainPlan(nodes, V, dim){
  const par = d => ((d.meta && d.meta.parent_drawer_id) || String(d.id).split("_chunk_")[0]);
  const groups = new Map();
  nodes.forEach((d, i) => {
    const p = par(d); let g = groups.get(p);
    if(!g){ g = {id: p, idx: [], wing: d.wing, room: d.room, filed: d.filed_at || "", text: ""}; groups.set(p, g); }
    g.idx.push(i); g.text += (d.content || "") + "\n";
    if(d.filed_at && (!g.filed || d.filed_at < g.filed)) g.filed = d.filed_at;
  });
  const P = [...groups.values()], n = P.length, byId = new Map(P.map((g, a) => [g.id, a]));
  const M = new Float32Array(n * dim);
  P.forEach((g, a) => { const o = a * dim;
    for(const i of g.idx){ const q = i * dim; for(let k = 0; k < dim; k++) M[o+k] += V[q+k]; }
    let s = 0; for(let k = 0; k < dim; k++) s += M[o+k] * M[o+k]; s = Math.sqrt(s) || 1; for(let k = 0; k < dim; k++) M[o+k] /= s; });
  const sim = (a, b) => { let s = 0; const oa = a * dim, ob = b * dim; for(let k = 0; k < dim; k++) s += M[oa+k] * M[ob+k]; return s; };
  const edges = [];                                        // {from: older, to: newer, kind: "says" | "likely", score, why}
  const seen = new Set(), add = (a, b, kind, score, why) => { const k = a + ">" + b; if(seen.has(k)) return; seen.add(k); edges.push({from: a, to: b, kind, score: +score.toFixed(2), why}); };
  const day = g => (g.filed || "").slice(0, 10);
  P.forEach((g, a) => {
    // 1. The drawer says it replaces something: read the words after the verb for a date or version to look for.
    const t = g.text, claims = [];
    const rx = new RegExp(CH_CLAIM.source, "gi"); let m;
    while((m = rx.exec(t)) && claims.length < 4){ claims.push(t.slice(m.index, m.index + 170)); }
    for(const cl of claims){
      const dates = new Set(cl.match(CH_DATE) || []), vers = new Set((cl.match(CH_VER) || []).filter(v => v.includes(".") && !/^\d{4}\./.test(v)));
      let best = null;
      for(let b = 0; b < n; b++){
        if(b === a || (P[b].filed || "") >= (g.filed || "")) continue;   // the replaced drawer was filed earlier (same day counts)
        if(P[b].wing !== g.wing && !dates.size && !vers.size) continue;
        const s = sim(a, b); if(s < 0.62) continue;
        const bt = P[b].text; let hit = 0;
        for(const d of dates) if(bt.includes(d) || day(P[b]) === d) hit++;
        for(const v of vers) if(bt.includes(v)) hit++;
        const score = s + 0.18 * Math.min(2, hit) + (P[b].room === g.room ? 0.06 : 0) + (P[b].wing === g.wing ? 0.05 : 0);
        if(!best || score > best.score) best = {b, score, hit, s};
      }
      if(best && best.score >= 0.8) add(best.b, a, "says", best.score, "this drawer says it replaces an older one" + (best.hit ? "; it shares " + best.hit + " date/version from that sentence" : "") + "; " + Math.round(best.s * 100) + "% alike");
    }
    // 2. The drawer is marked SUPERSEDED itself: the closest later drawer in the same wing is probably what replaced it.
    if(/\bSUPERSEDED\b/.test(t.slice(0, 400)) && !/supersedes/i.test(t.slice(0, 120))){
      let best = null;
      for(let b = 0; b < n; b++){ if(b === a || P[b].wing !== g.wing || (P[b].filed || "") <= (g.filed || "")) continue; const s = sim(a, b); if(s >= 0.7 && (!best || s > best.s)) best = {b, s}; }
      if(best) add(a, best.b, "likely", best.s, "marked SUPERSEDED; the closest later drawer in this wing (" + Math.round(best.s * 100) + "% alike)");
    }
  });
  // 3. Successive versions with no words about it: same room, very close in meaning, one after the other in time.
  const rooms = new Map(); P.forEach((g, a) => { const k = g.wing + "/" + g.room; (rooms.get(k) || rooms.set(k, []).get(k)).push(a); });
  for(const [k, ix] of rooms){
    if(ix.length < 3) continue;
    ix.sort((x, y) => day(P[x]).localeCompare(day(P[y])) || x - y);
    for(let u = 0; u < ix.length; u++){
      let best = null;
      for(let v = u + 1; v < ix.length && v < u + 8; v++){ if(day(P[ix[v]]) === day(P[ix[u]])) continue; const s = sim(ix[u], ix[v]); if(s >= 0.86 && (!best || s > best.s)) best = {b: ix[v], s}; }
      if(best) add(ix[u], best.b, "likely", best.s, "next close version in the same room (" + Math.round(best.s * 100) + "% alike)");
    }
  }
  const older = new Map(), newer = new Map();
  edges.forEach(e => { (newer.get(e.from) || newer.set(e.from, []).get(e.from)).push(e); (older.get(e.to) || older.set(e.to, []).get(e.to)).push(e); });
  return {P, byId, edges, older, newer, n, ids: P.map(g => g.id), meta: P.map(g => ({id: g.id, wing: g.wing, room: g.room, day: day(g), title: (g.text.split("\n").find(s => s.trim()) || "").trim().slice(0, 80), idx0: g.idx[0]}))};
}
// the chain through one drawer: follow explicit edges first, inferred ones only when asked
function chainOf(plan, a, depth, explicitOnly){
  const pick = (m, k) => { const es = (m.get(k) || []).filter(e => !explicitOnly || e.kind === "says"); es.sort((x, y) => (x.kind === "says" ? 0 : 1) - (y.kind === "says" ? 0 : 1) || y.score - x.score); return es[0]; };
  const back = [], fwd = [], seen = new Set([a]);
  for(let cur = a, d = 0; d < depth; d++){ const e = pick(plan.older, cur); if(!e || seen.has(e.from)) break; seen.add(e.from); back.unshift({a: e.from, edge: e}); cur = e.from; }
  for(let cur = a, d = 0; d < depth; d++){ const e = pick(plan.newer, cur); if(!e || seen.has(e.to)) break; seen.add(e.to); fwd.push({a: e.to, edge: e}); cur = e.to; }
  return {back, fwd};
}
// CHAIN-CORE-END

// CHAIN-UI-BEGIN
function gxChainEnsure(){
  if(!G.V || !G.nodes) return null;
  if(!G.chain || G.chainFor !== G.n){ try{ G.chain = chainPlan(G.nodes, G.V, G.V.length / G.n); G.chainFor = G.n; }catch(e){ G.chain = null; } }
  return G.chain;
}
function gxSupCompute(){                           // chunks of every drawer that a newer drawer says it replaces
  const pl = gxChainEnsure(); G.sup = new Uint8Array(G.n); if(!pl) return;
  pl.edges.forEach(e => { if(e.kind === "says") pl.P[e.from].idx.forEach(i => { G.sup[i] = 1; }); });
}
function gxChainFor(gi){                             // the drawers before and after this one, oldest first, with how each link was found
  const pl = gxChainEnsure(); if(!pl || gi < 0 || !G.nodes[gi]) return null;
  const d = G.nodes[gi], a = pl.byId.get((d.meta && d.meta.parent_drawer_id) || String(d.id).split("_chunk_")[0]);
  if(a == null) return null;
  const c = chainOf(pl, a, 6, false), steps = [...c.back, {a, me: true}, ...c.fwd], kinds = [];
  c.back.forEach(s => kinds.push(s.edge.kind)); c.fwd.forEach(s => kinds.push(s.edge.kind));
  return {kinds, path: steps.map(s => { const m = pl.meta[s.a]; return {a: s.a, gi: m.idx0, day: m.day, wing: m.wing, room: m.room, title: m.title, me: !!s.me}; })};
}
function gxChainPanel(){
  const el = document.getElementById("gxChain"); if(!el) return;
  const tc = (G.target >= 0) ? G.tchain : null;
  const key = tc ? JSON.stringify([G.target, !!G.privacy, tc.path.map(s => s.gi)]) : "";
  if(el.dataset.k === key && el.dataset.s === "1") return;        // per-element cache: the canvas is rebuilt when the view changes
  el.dataset.k = key; el.dataset.s = "1";
  if(!tc){ el.style.display = "none"; el.innerHTML = ""; return; }
  el.style.display = "";
  const T = s => G.privacy ? "" : esc(pv(s));
  if(tc.path.length < 2){ el.innerHTML = `<div class="ch-h">Supersede chain</div><div class="ch-n">No replaced or later version found for this drawer.</div>`; return; }
  el.innerHTML = `<div class="ch-h">Supersede chain · oldest first</div>` + tc.path.map((s, i) =>
    (i ? `<div class="ch-e">${tc.kinds[i-1] === "says" ? "↓ the newer drawer says it replaces this" : "↓ looks like a later version"}</div>` : "")
    + `<div class="ch-r${s.me ? " me" : ""}" data-gi="${s.gi}" title="Click to show this drawer on the map">${esc(s.day)} · ${esc(s.wing)}/${esc(s.room)}<br>${s.me ? "<b>this drawer</b> · " : ""}${T(s.title)}</div>`).join("");
  el.querySelectorAll(".ch-r").forEach(r => r.onclick = () => { const gi = +r.dataset.gi; const di = DATA.drawers.indexOf(G.nodes[gi]); if(di >= 0) gxSetTarget(di, true); });
}
function gxHistoryChain(d){                           // the same chain, as a list under the drawer's own history
  const box = document.getElementById("chainBox"); if(!box) return;
  const gi = (G.idxOf && G.idxOf.has(d.id)) ? G.idxOf.get(d.id) : -1, tc = gi >= 0 ? gxChainFor(gi) : null;
  if(!tc){ box.innerHTML = ""; return; }
  if(tc.path.length < 2){ box.innerHTML = `<div class="rv-h">Supersede chain</div><div class="rv-n">No replaced or later version found for this drawer.</div>`; return; }
  box.innerHTML = `<div class="rv-h">Supersede chain · oldest first</div>` + tc.path.map((s, i) =>
    (i ? `<div class="rv-n" style="margin:2px 0">${tc.kinds[i-1] === "says" ? "↓ the newer drawer says it replaces this" : "↓ looks like a later version"}</div>` : "")
    + `<div class="rv ${s.me ? "cur" : "old"}" ${s.me ? "" : `data-gi="${s.gi}" style="cursor:pointer"`}><b>${esc(s.day)}</b> · ${esc(s.wing)}/${esc(s.room)}${s.me ? " · <b>this drawer</b>" : ""}<div class="rv-p">${esc(pv(s.title))}</div></div>`).join("")
    + `<div class="rv-n">Chains are inferred: from a drawer's own words ("supersedes the 2026-10-12 wrap") plus how close in meaning and date two drawers are. Treat as a lead, not proof.</div>`;
  box.querySelectorAll("[data-gi]").forEach(r => r.onclick = () => detail(G.nodes[+r.dataset.gi]));
}
// CHAIN-UI-END

// AUDIT-CORE-BEGIN
// Re-file planner. READ-ONLY: it reads what the browser already holds (drawer text + stored embedding vectors) and
// returns a PLAN that a person approves; MPB never applies it. Unit of work is the PARENT drawer (a drawer is stored
// as chunks; moving it moves all its chunks). Signals are plain and inspectable: nearest neighbours by meaning,
// topic words, room size, name spelling, near-duplicates. Everything is labelled with WHY and how sure.
const AU_K = 8;
const AU_SNAKE = s => String(s || "").trim().toLowerCase().replace(/[\s\-]+/g, "_").replace(/[^a-z0-9_]/g, "");
const AU_TOPICS = [
  // [domain, home wing, regexp, minimum distinct hits]
  ["health", "health", /\b(dialysis|fistula|transplant|nephrolog\w*|diabet\w*|insulin|prescription|statin|dosage|medication|cardiolog\w*|oncolog\w*|cannulation)\b/gi, 2],
  ["financial", "financial", /\b(foreclos\w*|mortgage|lender|escrow|bankruptcy|refinanc\w*|credit score|tax return|401k|IRS|home sale|property tax|short sale)\b/gi, 2],
  ["family", "personal", /\b(grandbaby|grandchild\w*|my (?:wife|daughter|son|niece|nephew|brother|sister|mother|father|partner)|sunburned partner)\b/gi, 1]];
const AU_ADDR = /\b\d{2,5}\s+(?:(?:North|South|East|West|N|S|E|W)\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Lane|Ln|Court|Ct|Way|Blvd|Boulevard)\b/;
const AU_ADDR_G = new RegExp(AU_ADDR.source, "g");
const auTitle = (text, len) => ((String(text).split("\n").find(x => x.trim()) || "").trim().replace(AU_ADDR_G, "[address]")).slice(0, len);   // a plan must not spread an address
const AU_BY_TYPE = /^(diary|journal)$/i;          // rooms that are cross-topic on purpose; neighbours elsewhere is expected
const AU_WORKFACING = new Set(["work", "daily", "notes", "tech", "general", "lab"]);

function auditPlan(nodes, V, dim){
  const par = d => ((d.meta && d.meta.parent_drawer_id) || String(d.id).split("_chunk_")[0]);
  const groups = new Map();
  nodes.forEach((d, i) => {
    const p = par(d); let g = groups.get(p);
    if(!g){ g = {id: p, idx: [], wing: d.wing, room: d.room, by: d.added_by || "", src: d.source_file || "", filed: d.filed_at || "", text: ""}; groups.set(p, g); }
    g.idx.push(i); g.text += (d.content || "") + "\n";
    if(d.filed_at && (!g.filed || d.filed_at < g.filed)) g.filed = d.filed_at;
  });
  const P = [...groups.values()], n = P.length;
  // mean unit vector per parent
  const M = new Float32Array(n * dim);
  P.forEach((g, a) => {
    const o = a * dim;
    for(const i of g.idx){ const q = i * dim; for(let k = 0; k < dim; k++) M[o+k] += V[q+k]; }
    let s = 0; for(let k = 0; k < dim; k++) s += M[o+k] * M[o+k]; s = Math.sqrt(s) || 1;
    for(let k = 0; k < dim; k++) M[o+k] /= s;
  });
  // K nearest parents
  const nb = new Int32Array(n * AU_K).fill(-1), ns = new Float32Array(n * AU_K).fill(-2);
  for(let a = 0; a < n; a++){
    const oa = a * dim;
    for(let b = a + 1; b < n; b++){
      const ob = b * dim; let s = 0; for(let k = 0; k < dim; k++) s += M[oa+k] * M[ob+k];
      for(const [x, y] of [[a, b], [b, a]]){
        const o = x * AU_K; if(s <= ns[o+AU_K-1]) continue;
        let p = AU_K - 1; while(p > 0 && ns[o+p-1] < s){ ns[o+p] = ns[o+p-1]; nb[o+p] = nb[o+p-1]; p--; }
        ns[o+p] = s; nb[o+p] = y;
      }
    }
  }
  const roomN = new Map(); P.forEach(g => { const k = AU_SNAKE(g.wing) + "/" + g.room; roomN.set(k, (roomN.get(k) || 0) + 1); });
  const moves = [], keep = [], sensitive = [], dups = [];
  P.forEach((g, a) => {
    const wing = AU_SNAKE(g.wing), title = auTitle(g.text, 80) || g.id;
    // neighbour context (weighted by similarity), skipping nothing: siblings are other parents
    const wc = new Map(), rc = new Map(); let tot = 0;
    for(let q = 0; q < AU_K; q++){
      const b = nb[a*AU_K+q]; if(b < 0) continue; const w = Math.max(0.01, ns[a*AU_K+q]);
      const nw = AU_SNAKE(P[b].wing); wc.set(nw, (wc.get(nw) || 0) + w);
      const key = nw + "/" + P[b].room; rc.set(key, (rc.get(key) || 0) + w); tot += w;
    }
    const topW = [...wc.entries()].sort((x, y) => y[1] - x[1])[0], share = topW ? topW[1] / tot : 0;
    // topic words
    const hits = {}; for(const [dom, home, rx, minHits] of AU_TOPICS){
      const set = new Set((g.text.match(rx) || []).map(s => s.toLowerCase())); if(set.size >= minHits) hits[dom] = {home, n: set.size, words: [...set].slice(0, 4)};
    }
    const addr = AU_ADDR.test(g.text);
    if(addr) sensitive.push({parent: g.id, wing: g.wing, room: g.room, title, kind: "address-shaped text"});
    let mv = null;
    const dom = Object.entries(hits).sort((x, y) => y[1].n - x[1].n)[0];
    const isRef = (g.text.match(/https?:\/\//g) || []).length >= 5 || /index/i.test(g.room) || /^\W*\w+ (accessible )?index\b/i.test(title);        // an index or link list mentions topics without being about them
    const isConv = /wb_sync|Source: Bee conversation/i.test(g.src + " " + g.text.slice(0, 400));
    if(dom && dom[1].home !== wing){
      // a personal topic sitting in a work-facing wing is the case that matters most
      mv = {to: dom[1].home, room: isConv ? "conversations" : "notes", why: dom[0] + " words (" + dom[1].words.join(", ") + ") in " + g.wing,
            conf: isRef ? "low" : (AU_WORKFACING.has(wing) ? "high" : "medium")};
    } else if(!AU_BY_TYPE.test(g.room) && !/^wing_/.test(wing) && topW && topW[0] !== wing && share >= 0.7){
      const best = [...rc.entries()].filter(e => e[0].startsWith(topW[0] + "/")).sort((x, y) => y[1] - x[1])[0];
      mv = {to: topW[0], room: best ? best[0].split("/").slice(1).join("/") : "general",
            why: Math.round(share * 100) + "% of its closest drawers are in " + topW[0], conf: share >= 0.85 ? "medium" : "low"};
    }
    if(mv) moves.push({parent: g.id, chunks: g.idx.length, title, from: {wing: g.wing, room: g.room}, to: {wing: mv.to, room: mv.room}, why: mv.why, conf: mv.conf, by: g.by, filed: g.filed.slice(0, 10)});
    else keep.push(g.id);
    for(let q = 0; q < AU_K; q++){ const b = nb[a*AU_K+q]; if(b > a && ns[a*AU_K+q] >= 0.97) dups.push({a: g.id, b: P[b].id, sim: +ns[a*AU_K+q].toFixed(3), wa: g.wing + "/" + g.room, wb: P[b].wing + "/" + P[b].room}); }
  });
  // spelling: wing names that are not already lowercase snake_case
  const ren = new Map(); P.forEach(g => { const s = AU_SNAKE(g.wing); if(s !== g.wing){ const k = g.wing + "→" + s; ren.set(k, (ren.get(k) || 0) + 1); } });
  const renames = [...ren.entries()].map(([k, c]) => { const [from, to] = k.split("→"); return {from, to, drawers: c}; }).sort((x, y) => y.drawers - x.drawers);
  // room spelling: lowercase, words joined by hyphens (the convention most rooms already follow)
  const AU_ROOM = r => String(r || "").trim().toLowerCase().replace(/[\s_]+/g, "-");
  const rr = new Map(); P.forEach(g => { const s = AU_ROOM(g.room); if(s !== g.room){ const k = AU_SNAKE(g.wing) + "|" + g.room + "|" + s; rr.set(k, (rr.get(k) || 0) + 1); } });
  const roomRenames = [...rr.entries()].map(([k, c]) => { const [w, from, to] = k.split("|"); return {wing: w, from, to, drawers: c}; });
  // rooms too small to be a room: where would their drawers' neighbours put them?
  const tiny = [];
  const byRoom = new Map(); P.forEach((g, a) => { const k = AU_SNAKE(g.wing) + "/" + g.room; (byRoom.get(k) || byRoom.set(k, []).get(k)).push(a); });
  for(const [k, ix] of byRoom){
    if(ix.length > 2) continue;
    const w = k.split("/")[0], votes = new Map();
    for(const a of ix) for(let q = 0; q < AU_K; q++){ const b = nb[a*AU_K+q]; if(b < 0) continue; const key = AU_SNAKE(P[b].wing) + "/" + P[b].room; if(key !== k && (roomN.get(key) || 0) > 2 && key.startsWith(w + "/")) votes.set(key, (votes.get(key) || 0) + ns[a*AU_K+q]); }
    const best = [...votes.entries()].sort((x, y) => y[1] - x[1])[0];
    tiny.push({room: k, drawers: ix.length, title: auTitle(P[ix[0]].text, 60), mergeInto: best ? best[0] : null});
  }
  const byConf = {high: 0, medium: 0, low: 0}; moves.forEach(m => byConf[m.conf]++);
  return {parents: n, chunks: nodes.length, rooms: roomN.size, moves, byConf, keep: keep.length, renames, roomRenames, tiny: tiny.sort((x, y) => x.drawers - y.drawers), sensitive, dups,
          all: P.map(g => { const m = moves.find(x => x.parent === g.id); return {id: g.id, chunks: g.idx.length, wing: g.wing, room: g.room, title: auTitle(g.text, 60), by: g.by, filed: g.filed.slice(0, 10),
            decision: m ? "move" : "keep", to: m ? m.to : null, why: m ? m.why : "", conf: m ? m.conf : ""}; })};
}
// AUDIT-CORE-END

// AUDIT-UI-BEGIN
async function auditOpen(){
  sheet(`<h2>Re-file planner</h2><p class="sub">Reading each drawer's stored meaning and comparing it with where it is filed…</p>`);
  if(!G.V){
    if(G.state !== "loading" && G.state !== "layout"){ try{ await gxLoad(); }catch(e){} }
    for(let t = 0; t < 120 && !G.V; t++) await new Promise(r => setTimeout(r, 500));
  }
  if(!G.V){ sheet(`<h2>Re-file planner</h2><p class="sub">It needs the stored embeddings, which could not be loaded. Open the Galaxy once and try again.</p>`); return; }
  if(!G.plan || G.planFor !== G.n){ G.plan = auditPlan(G.nodes, G.V, G.V.length / G.n); G.planFor = G.n; }
  auditPlanSheet();
}
function auditExport(){
  const p = G.plan, pick = m => ({parent: m.parent, from: m.from, to: m.to, why: m.why, confidence: m.conf, text_md5: (DATA.phash || {})[m.parent] || null});
  return JSON.stringify({
    generated: new Date().toISOString(), mpb_version: DATA.mpb_version, read_only: true,
    palace: {chunks: DATA.count, parents: p.parents, read_at: DATA.read_at},
    contains: "moves only: no text edits",
    text_md5_method: "md5 of the drawer text with all whitespace removed, chunks joined in chunk_index order (hashlib.md5)",
    before_applying: ["re-read each drawer and compute text_md5 the same way; if it differs from the plan, SKIP that move and report it",
                      "take a backup of the palace first and apply in batches a person has approved",
                      "MemPalace 3.12 lowercases wing names on write; use lowercase snake_case wings"],
    note: "A PLAN to review, not an instruction. MemPalace Browser never applies it. Apply with mempalace_update_drawer (wing=, room=) in approved batches, after a backup. Wing names are lowercased by MemPalace 3.12 on write.",
    summary: {parents: p.parents, chunks: p.chunks, rooms: p.rooms, moves: p.moves.length, by_confidence: p.byConf, wing_renames: p.renames.length, room_renames: p.roomRenames.length, small_rooms: p.tiny.length},
    moves: p.moves.map(pick), wing_renames: p.renames, room_renames: p.roomRenames,
    small_rooms: p.tiny.map(t => ({room: t.room, drawers: t.drawers, suggested_merge_into: t.mergeInto})),
    sensitive_flags: p.sensitive.map(s => ({parent: s.parent, wing: s.wing, room: s.room, kind: s.kind})),
    every_parent_drawer: p.all.map(r => ({id: r.id, chunks: r.chunks, wing: r.wing, room: r.room, decision: r.decision, to: r.to, confidence: r.conf, text_md5: (DATA.phash || {})[r.id] || null}))
  }, null, 1);
}
function auditPlanSheet(){
  const p = G.plan, hide = !!G.privacy, T = s => hide ? "(hidden: privacy on)" : esc(pv(s));
  const badge = c => `<span style="font-size:10.5px;padding:1px 6px;border-radius:9px;border:1px solid var(--line-2);color:${c === "high" ? "var(--warning)" : "var(--fg-subtle)"}">${esc(c)}</span>`;
  const mrow = m => `<div class="cf"><div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${T(m.title)}</div>
      <div style="color:var(--fg-subtle);font-size:11.5px">${esc(m.from.wing)} / ${esc(m.from.room)} → <b style="color:var(--fg)">${esc(m.to.wing)} / ${esc(m.to.room)}</b> · ${badge(m.conf)} · ${esc(m.why)} · filed ${esc(m.filed)} · ${m.chunks} chunk${m.chunks === 1 ? "" : "s"}</div></div>`;
  const high = p.moves.filter(m => m.conf === "high"), rest = p.moves.filter(m => m.conf !== "high");
  const grp = new Map(); rest.forEach(m => { const k = m.from.wing + " / " + m.from.room + " → " + m.to.wing + " / " + m.to.room; (grp.get(k) || grp.set(k, []).get(k)).push(m); });
  sheet(`<h2>Re-file planner</h2>
    <p class="sub">A read-only plan: where each drawer is filed, against where its neighbours in meaning, its topic words, its spelling and its room size say it belongs.
      These are suggestions with reasons, not verdicts. <b>This browser never applies them</b>; a person approves each batch and an agent applies it through MemPalace, after a backup.</p>
    <h3>Summary</h3>
    <dl class="kv">
      <dt>Looked at</dt><dd>${p.parents} drawers (${p.chunks.toLocaleString()} stored chunks) in ${p.rooms} rooms</dd>
      <dt>Suggested moves</dt><dd>${p.moves.length} (${p.byConf.high} high · ${p.byConf.medium} medium · ${p.byConf.low} low); the other ${p.keep} look right where they are</dd>
      <dt>Wing spelling</dt><dd>${p.renames.length} wings are not lowercase snake_case (MemPalace 3.12 lowercases on write, so they keep spawning twins)</dd>
      <dt>Room spelling</dt><dd>${p.roomRenames.length} rooms differ from lowercase-hyphen</dd>
      <dt>Small rooms</dt><dd>${p.tiny.length} rooms hold one or two drawers</dd>
      <dt>Sensitive shapes</dt><dd>${p.sensitive.length} drawers contain address-shaped text; ${p.dups.length} near-duplicate pairs</dd>
    </dl>
    <div style="margin:10px 0"><button id="auCopy" class="rv-more" style="border:1px solid var(--line-2);padding:4px 10px;border-radius:6px">Copy plan (JSON) for your agent</button>
      <button id="auDl" class="rv-more" style="border:1px solid var(--line-2);padding:4px 10px;border-radius:6px;margin-left:6px">Download plan.json</button>
      <span id="auMsg" class="sub" style="margin-left:8px"></span></div>
    <h3>Personal topics in the wrong place (${high.length})</h3>
    <p class="sub">Health, money or family words in a work-facing wing. Review these first.</p>
    ${high.length ? high.map(mrow).join("") : `<div class="sub">None.</div>`}
    <h3>Other suggested moves (${rest.length})</h3>
    ${[...grp.entries()].map(([k, v]) => `<details><summary style="cursor:pointer;margin:6px 0">${v.length} · ${esc(k)}</summary>${v.map(mrow).join("")}</details>`).join("") || `<div class="sub">None.</div>`}
    <h3>Wing spelling (${p.renames.length})</h3>
    <dl class="kv">${p.renames.map(r => `<dt>${esc(r.from)}</dt><dd>→ ${esc(r.to)} · ${r.drawers} drawers</dd>`).join("")}</dl>
    <h3>Room spelling (${p.roomRenames.length})</h3>
    <dl class="kv">${p.roomRenames.map(r => `<dt>${esc(r.wing)} / ${esc(r.from)}</dt><dd>→ ${esc(r.to)} · ${r.drawers}</dd>`).join("")}</dl>
    <h3>Small rooms (${p.tiny.length})</h3>
    <p class="sub">One or two drawers. A suggestion is the closest bigger room in the same wing, or none if nothing is close.</p>
    <dl class="kv">${p.tiny.map(t => `<dt>${esc(t.room)} (${t.drawers})</dt><dd>${t.mergeInto ? "→ " + esc(t.mergeInto) : "no close room"} <span style="color:var(--fg-subtle)">· ${T(t.title)}</span></dd>`).join("")}</dl>
    <h3>Address-shaped text (${p.sensitive.length})</h3>
    <dl class="kv">${p.sensitive.map(s => `<dt>${esc(s.wing)} / ${esc(s.room)}</dt><dd>${esc(s.parent)}</dd>`).join("") || "<dt>none</dt><dd></dd>"}</dl>`);
  const msg = t => { const m = document.getElementById("auMsg"); if(m) m.textContent = t; };
  document.getElementById("auCopy").onclick = async () => {
    const txt = auditExport();
    if(await copyText(txt)){ msg("copied"); return; }
    msg("the browser blocked copying: the plan is selected below, press Ctrl/Cmd+C, or use Download");   // last resort: a selected text box
    let ta = document.getElementById("auText");
    if(!ta){ ta = document.createElement("textarea"); ta.id = "auText"; ta.readOnly = true; ta.style.cssText = "width:100%;height:120px;margin-top:8px;font:11px var(--mono);background:var(--bg);color:var(--fg);border:1px solid var(--line-2);border-radius:6px";
      document.getElementById("auMsg").parentElement.after(ta); }
    ta.value = txt; ta.focus(); ta.select();
  };
  document.getElementById("auDl").onclick = () => {
    const a = document.createElement("a"), u = URL.createObjectURL(new Blob([auditExport()], {type: "application/json"}));
    a.href = u; a.download = "mpb-refile-plan.json"; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 1000); msg("downloaded");
  };
}
// AUDIT-UI-END

function auditSheet(){
  const a = DATA.audit || {};
  if(!a.available){
    sheet(`<h2>Palace audit</h2><p class="sub">${esc(a.reason || "not available")}</p>`);
    return;
  }
  const r = a.report || {}, s = r.scores || {}, rm = r.rooms || {}, nm = r.naming || {}, tn = r.tunnels || {},
        hl = r.hallways || {}, kg = r.knowledge_graph || {};
  const issues = auditIssues();
  const line = (label, v, why) => `<dt>${label}</dt><dd><span style="color:${scoreColor(v)}">${v == null ? "n/a" : esc(String(v))}</span> — ${why}</dd>`;
  sheet(`
    <h2>Palace audit — <span style="color:${scoreColor(s.overall)}">${esc(String(s.overall))} / 100</span></h2>
    <p class="sub">MemPalace's own organisation check, run by this browser through the package so the two cannot
      disagree. It scores layout, not content, and it never changes anything.</p>
    <h3>Scores</h3>
    <dl class="kv">
      ${line("Rooms", s.rooms, `${esc(String(rm.generic_drawers))} of ${esc(String(rm.total_drawers))} drawers sit in generic rooms`)}
      ${line("Naming", s.naming, `${(nm.wing_drift || []).length + (nm.room_drift || []).length + (nm.mixed_wings || []).length} colliding spellings`)}
      ${line("Tunnels", s.tunnels, `${esc(String(tn.total))} tunnels, ${esc(String(tn.never_traversed))} never followed`)}
      ${line("Hallways", s.hallways, `${esc(String(hl.total))} hallways`)}
      ${line("Knowledge graph", s.knowledge_graph, `${esc(String(kg.current_facts))} current facts, ${esc(String(kg.distinct_predicates))} predicates, ${esc(String(kg.one_off_predicates))} used once`)}
    </dl>
    <h3>Findings (${issues.length})</h3>
    ${issues.length ? issues.map((i, n) => advisoryHtml(i, n)).join("") : `<div class="sub" style="margin:0">Nothing to report.</div>`}
    <h3>Run the same check yourself</h3>
    <pre class="fx-c">mempalace audit</pre>`);
  wireAdvisories(issues);
}

// ---- galaxy: every drawer a dot, placed by what it MEANS -------------------------
// The positions come from the drawers' stored embeddings: each drawer is joined to its
// nearest neighbours by similarity, and a force simulation pulls those neighbours together
// while pushing everything else apart. Nothing here is written anywhere.
// GX-COMPUTE-BEGIN
function gxDecode(b64, n, dim){
  const bin = atob(b64), len = bin.length, buf = new Uint8Array(len);
  for(let i = 0; i < len; i++) buf[i] = bin.charCodeAt(i);
  const V = new Float32Array(buf.buffer);
  if(V.length !== n * dim) throw new Error("vector size mismatch: " + V.length + " vs " + (n * dim));
  for(let i = 0; i < n; i++){                       // unit length, so a dot product is a cosine
    let s = 0; const o = i * dim;
    for(let k = 0; k < dim; k++) s += V[o+k] * V[o+k];
    s = Math.sqrt(s) || 1;
    for(let k = 0; k < dim; k++) V[o+k] /= s;
  }
  return V;
}
async function gxKnn(V, n, dim, K, progress){
  const nb = new Int32Array(n * K).fill(-1), ns = new Float32Array(n * K).fill(-2);
  const put = (i, j, s) => {                        // keep the K best, best first
    const o = i * K;
    if(s <= ns[o+K-1]) return;
    let p = K - 1;
    while(p > 0 && ns[o+p-1] < s){ ns[o+p] = ns[o+p-1]; nb[o+p] = nb[o+p-1]; p--; }
    ns[o+p] = s; nb[o+p] = j;
  };
  let t0 = performance.now();
  for(let i = 0; i < n; i++){
    const oi = i * dim;
    for(let j = i + 1; j < n; j++){
      const oj = j * dim; let s = 0;
      for(let k = 0; k < dim; k++) s += V[oi+k] * V[oj+k];
      put(i, j, s); put(j, i, s);
    }
    if(performance.now() - t0 > 14){                // yield so the page stays alive
      if(progress) progress(i / n);
      await new Promise(r => setTimeout(r, 0));
      t0 = performance.now();
    }
  }
  return {nb, ns};
}
function gxInit(V, n, dim){
  let s = 0x9E3779B9;
  const rnd = () => { s = (s + 0x6D2B79F5) >>> 0; let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const gauss = () => Math.sqrt(-2 * Math.log(Math.max(rnd(), 1e-9))) * Math.cos(2 * Math.PI * rnd());
  const R = new Float32Array(dim * 2);
  for(let k = 0; k < dim * 2; k++) R[k] = gauss();
  const pos = new Float32Array(n * 2);              // start from two random directions, so similar drawers
  for(let i = 0; i < n; i++){                       // already begin near each other
    let x = 0, y = 0; const o = i * dim;
    for(let k = 0; k < dim; k++){ x += V[o+k] * R[2*k]; y += V[o+k] * R[2*k+1]; }
    pos[2*i] = x * 160 + (rnd() - .5) * 8; pos[2*i+1] = y * 160 + (rnd() - .5) * 8;
  }
  return {pos, vel: new Float32Array(n * 2), fx: new Float32Array(n), fy: new Float32Array(n), seed: 0x1234567};
}
// ---- collision candidates (reviewer's design, 2026-11-22). NOT a contradiction detector: similarity cannot tell
// agreement from disagreement. It takes very close neighbours, pulls typed values (dates, versions, IPs, counts,
// "N of M", dollar amounts) with plain regexes, and flags a pair only when both state the same kind of slot
// (same kind + same label word) with different values. Wording stays humble: "possible conflict - values differ".
// Dated snapshots of one evolving document (successive handoffs) are expected to differ; they are listed apart.
const CF_MIN = 0.75;
const CF_MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec";
const CF_DATE = new RegExp("\\b20\\d\\d-\\d\\d-\\d\\d\\b|\\b(?:" + CF_MON + ")[a-z]* \\d{1,2}\\b", "i");
const CF_RX = [
  ["date", new RegExp("(?:([A-Za-z][\\w.-]{2,})\\W{1,3})?(?:\\b(20\\d\\d-\\d\\d-\\d\\d)\\b|\\b((?:" + CF_MON + ")[a-z]*\\.? \\d{1,2})\\b)", "gi")],
  ["version", /([A-Za-z][\w.-]{2,})\W{0,3}v?(\d+\.\d+(?:\.\d+)?(?:-dev\.\d+)?)\b(?!\.\d)/g],
  ["ip", /([A-Za-z][\w.-]{2,})\W{0,4}(\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b)/g],
  ["of", /\b(\d+) of (\d+)\b/g],
  ["count", /\b(\d[\d,]*) (drawers|nodes|VMs?|wings|rooms|tests|entries|items|hosts|findings|warnings|backups|triples|facts)\b/gi],
  ["money", /\$(\d[\d,]*(?:\.\d+)?)/g]];
function cfSlots(t){
  const out = new Map(), add = (k, v) => { let s = out.get(k); if(!s){ s = new Set(); out.set(k, s); } s.add(v); };
  for(const [kind, rx] of CF_RX){
    rx.lastIndex = 0;
    for(const m of t.matchAll(rx)){
      if(kind === "date"){ const key = (m[1] || "").toLowerCase(); if(!key) continue; add("date|" + key, (m[2] || m[3]).toLowerCase()); }
      else if(kind === "version" || kind === "ip") add(kind + "|" + m[1].toLowerCase(), m[2]);
      else if(kind === "of") add("of|", m[1] + "/" + m[2]);
      else if(kind === "count") add("count|" + m[2].toLowerCase().replace(/s$/, ""), m[1].replace(/,/g, ""));
      else add("money|", m[1]);
    }
  }
  return out;
}
function gxConflictsInit(){
  if(G.cf || !G.nb || !G.ns) return;
  const n = G.n, K = G.K, slots = new Array(n), seen = new Set(), flags = [];
  const dated = i => CF_DATE.test((G.nodes[i].content || "").slice(0, 200));
  const parent = i => String(G.nodes[i].id).split("_chunk_")[0];
  for(let i = 0; i < n; i++){
    for(let q = 0; q < K; q++){
      const j = G.nb[i*K+q], s = G.ns[i*K+q];
      if(j < 0 || s < CF_MIN) continue;
      const a = Math.min(i, j), b = Math.max(i, j), key = a * n + b;
      if(seen.has(key)) continue; seen.add(key);
      if(parent(a) === parent(b)) continue;
      const sa = slots[a] || (slots[a] = cfSlots(G.nodes[a].content || "")), sb = slots[b] || (slots[b] = cfSlots(G.nodes[b].content || ""));
      const diff = [];
      for(const [k, va] of sa){
        const vb = sb.get(k); if(!vb) continue;
        let overlap = false; for(const x of va) if(vb.has(x)){ overlap = true; break; }
        if(!overlap) diff.push({k, a: [...va].slice(0, 3), b: [...vb].slice(0, 3)});
      }
      if(!diff.length) continue;
      const A = G.nodes[a], B = G.nodes[b];
      const snap = (dated(a) && dated(b)) ||
        (A.wing === B.wing && A.room === B.room && String(A.filed_at || "").slice(0, 10) !== String(B.filed_at || "").slice(0, 10));
      flags.push({a, b, s, diff, snap});
    }
  }
  flags.sort((x, y) => y.s - x.s);
  G.cf = {real: flags.filter(f => !f.snap), snaps: flags.filter(f => f.snap), pairs: seen.size};
  gxConfLabel();
}
function gxConfLabel(){
  const b = document.getElementById("gxConf"); if(!b || !G.cf) return;
  const r = confSplit(G.cf.real).n;
  b.style.display = ""; b.textContent = "≠ conflicts " + r;
  b.title = "Very close drawer pairs that give different values for the same thing (" + r + " to look at, " + G.cf.snaps.length + " dated snapshots set aside)";
}
function confSplit(list){                          // same room, same day, only counts differ: successive runs, grouped
  const gk = new Map(), single = [];
  for(const f of list){
    const A = G.nodes[f.a], B = G.nodes[f.b], dA = String(A.filed_at || "").slice(0, 10), dB = String(B.filed_at || "").slice(0, 10);
    if(A.wing === B.wing && A.room === B.room && dA === dB && f.diff.every(x => /^(count|of)\|/.test(x.k))){
      const k = A.wing + "/" + A.room + "|" + dA; (gk.get(k) || gk.set(k, []).get(k)).push(f);
    } else single.push(f);
  }
  const groups = [];
  for(const [k, fs] of gk){ if(fs.length < 2) single.push(...fs); else groups.push([k, fs]); }
  single.sort((x, y) => y.s - x.s);
  return {groups, single, n: groups.length + single.length};
}
function confRows(list, row){
  const sp = confSplit(list); let out = "";
  for(const [k, fs] of sp.groups){
    const [loc, day] = k.split("|"), vals = new Set();
    fs.forEach(f => f.diff.forEach(x => { x.a.forEach(v => vals.add(v)); x.b.forEach(v => vals.add(v)); }));
    out += `<details class="cf"><summary style="cursor:pointer"><span style="color:var(--warning)">same-day runs</span> · ${fs.length} pairs filed ${esc(day)} in ${esc(loc)} differ only in counts (${[...vals].slice(0, 8).map(esc).join(", ")}): successive runs, not a disagreement</summary>${fs.map(row).join("")}</details>`;
  }
  return out + sp.single.slice(0, 40).map(row).join("");
}
function conflictsSheet(){
  const C = G.cf; if(!C){ sheet(`<h2>Possible conflicts</h2><p class="sub">Still computing…</p>`); return; }
  const slotTxt = d => d.map(x => {
    const [kind, lab] = x.k.split("|");
    return `${esc(lab ? lab + " " : "")}${esc({date:"date", version:"version", ip:"address", of:"N of M", count:"count", money:"amount"}[kind] || kind)}: <b>${esc(x.a.join(", "))}</b> vs <b>${esc(x.b.join(", "))}</b>`;
  }).join("<br>");
  const side = (i) => { const d = G.nodes[i]; return `<div style="flex:1;min-width:0"><div style="color:var(--fg-subtle);font-size:11px">${esc(d.wing)} / ${esc(d.room)} · filed ${esc(String(d.filed_at || "").slice(0, 10))}</div>
      <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(pv(((d.content || "").split("\n").find(s => s.trim()) || d.id).trim().slice(0, 90)))}</div>
      <a class="cf-go" data-i="${i}" data-a="open" role="button" tabindex="0">open</a> · <a class="cf-go" data-i="${i}" data-a="map" role="button" tabindex="0">on map</a></div>`; };
  const row = f => `<div class="cf"><div style="display:flex;gap:14px">${side(f.a)}${side(f.b)}</div>
      <div style="margin-top:4px;font-size:12px"><span style="color:var(--warning)">possible conflict — values differ</span> · ${Math.round(f.s * 100)}% alike<br>${slotTxt(f.diff.slice(0, 3))}</div></div>`;
  sheet(`<h2>Possible conflicts (${confSplit(C.real).n})</h2>
    <p class="sub">Pairs of drawers that are very close in meaning and give <b>different values for the same thing</b> (a date, version, address, count or amount).
      A pattern match, not a verdict: it cannot tell which one is right, or whether both are true on their own day. Nothing here changes anything.</p>
    ${C.real.length ? confRows(C.real, row) : `<div class="sub">None right now.</div>`}
    ${C.snaps.length ? `<details style="margin-top:12px"><summary style="cursor:pointer;color:var(--fg-muted)">${C.snaps.length} dated snapshots set aside</summary>
      <p class="sub" style="margin-top:8px">Successive versions of one evolving document, such as handoffs. They are expected to differ, so they are not flagged as conflicts.</p>
      ${C.snaps.slice(0, 40).map(row).join("")}</details>` : ""}
    <p class="sub" style="margin-top:12px">${C.pairs.toLocaleString()} close pairs checked (similarity ${Math.round(CF_MIN * 100)}% or more).</p>`);
  document.querySelectorAll("#sheet .cf-go").forEach(el => {
    const go = () => { const i = +el.dataset.i; if(el.dataset.a === "open"){ closeSheet(); detail(G.nodes[i]); } else { closeSheet(); gxLocate(DATA.drawers.indexOf(G.nodes[i])); } };
    el.onclick = go; el.onkeydown = e => { if(e.key === "Enter" || e.key === " "){ e.preventDefault(); go(); } };
  });
}
// ---- cluster summary: a dense pile of dots from one room reads as a pile, with its size and span
function authorKey(a){                              // claude-code, Claude Code, CCode, "Claude Code (work)" are one author
  let s = String(a || "").trim().toLowerCase(); if(!s) return "";
  s = s.replace(/\s*\([^)]*\)/g, "").replace(/,\s*sequence.*$/, "").replace(/[\s_]+/g, "-").replace(/-+$/, "");
  if(/eod/.test(s)) return "eods";
  if(/^(claude-code|ccode)(-personal|-work)?$/.test(s)) return "claude-code";
  if(/^claude/.test(s)) return "claude";
  return s;
}
function gxRoomStats(){
  if(G.rs && G.rsFor === G.n) return G.rs;
  const m = new Map();
  G.nodes.forEach((d, i) => {
    const k = d.wing + "\u0001" + d.room; let s = m.get(k);
    if(!s) m.set(k, s = {n: 0, lo: Infinity, hi: -Infinity});
    s.n++; if(G.t[i]){ s.lo = Math.min(s.lo, G.t[i]); s.hi = Math.max(s.hi, G.t[i]); }
  });
  G.rs = m; G.rsFor = G.n; return m;
}
function gxSpan(lo, hi){
  if(!isFinite(lo) || !isFinite(hi)) return "";
  const a = new Date(lo), b = new Date(hi), mon = d => d.toLocaleDateString(undefined, {month: "short"});
  if(a.getFullYear() === b.getFullYear()) return a.getMonth() === b.getMonth() ? mon(a) + " " + a.getFullYear() : mon(a) + "–" + mon(b) + " " + a.getFullYear();
  return mon(a) + " " + a.getFullYear() + "–" + mon(b) + " " + b.getFullYear();
}
function gxCluster(h, mx, my){
  // dots within 28px of the pointer; a "pile" is 12 or more of them, mostly (60%+) from the hovered dot's own room
  let tot = 0, same = 0; const d = G.nodes[h];
  for(let i = 0; i < G.n; i++){
    if(!G.vis[i]) continue;
    const dx = G.sx[i] - mx, dy = G.sy[i] - my;
    if(dx * dx + dy * dy > 784) continue;
    tot++; if(G.nodes[i].wing === d.wing && G.nodes[i].room === d.room) same++;
  }
  if(tot < 12 || same / tot < 0.6) return null;
  const s = gxRoomStats().get(d.wing + "\u0001" + d.room);
  return s && s.n >= 12 ? {wing: d.wing, room: d.room, n: s.n, span: gxSpan(s.lo, s.hi)} : null;
}
function gxNoteRoom(d){                              // remember the room of the dot under the pointer; show a button for it
  const k = d.wing + "/" + d.room; if(G.privacy || (G.lastRoom && G.lastRoom.k === k)) return;
  const s = gxRoomStats().get(d.wing + "\u0001" + d.room); G.lastRoom = {k, wing: d.wing, room: d.room, n: s ? s.n : 0};
  const b = document.getElementById("gxListBtn");
  if(b){ b.style.display = ""; b.textContent = "☰ list " + k + " (" + G.lastRoom.n + ")"; b.title = "List this room by date (key L works when the search box is not focused)"; }
}
function gxListCluster(){
  const c = G.cluster || G.lastRoom; if(!c) return;
  sel = {wing: c.wing, room: c.room}; query = ""; const q = document.getElementById("q"); if(q) q.value = "";
  view = "drawers"; render();
}
function gxStep(P, nb, ns, n, K, alpha){
  const pos = P.pos, vel = P.vel, fx = P.fx, fy = P.fy, R = 46, R2 = R * R;
  fx.fill(0); fy.fill(0);
  const grid = new Map(), key = (cx, cy) => (cx * 73856093) ^ (cy * 19349663);
  for(let i = 0; i < n; i++){
    const kk = key(Math.floor(pos[2*i] / R), Math.floor(pos[2*i+1] / R));
    const a = grid.get(kk); if(a) a.push(i); else grid.set(kk, [i]);
  }
  for(let i = 0; i < n; i++){                       // push apart anything closer than R
    const xi = pos[2*i], yi = pos[2*i+1], cx = Math.floor(xi / R), cy = Math.floor(yi / R);
    for(let dx = -1; dx <= 1; dx++) for(let dy = -1; dy <= 1; dy++){
      const a = grid.get(key(cx + dx, cy + dy)); if(!a) continue;
      for(let q = 0; q < a.length; q++){
        const j = a[q]; if(j <= i) continue;
        let ddx = xi - pos[2*j], ddy = yi - pos[2*j+1], d2 = ddx * ddx + ddy * ddy;
        if(d2 >= R2) continue;
        if(d2 < 0.01){                              // exactly on top of each other: nudge, but deterministically
          P.seed = (Math.imul(P.seed, 1664525) + 1013904223) >>> 0; ddx = P.seed / 4294967296 - .5;
          P.seed = (Math.imul(P.seed, 1664525) + 1013904223) >>> 0; ddy = P.seed / 4294967296 - .5; d2 = 0.25;
        }
        const d = Math.sqrt(d2), f = (R - d) / R * 1.7;
        fx[i] += ddx / d * f; fy[i] += ddy / d * f; fx[j] -= ddx / d * f; fy[j] -= ddy / d * f;
      }
    }
  }
  for(let i = 0; i < n; i++) for(let q = 0; q < K; q++){   // pull nearest neighbours together
    const j = nb[i*K+q]; if(j < 0) continue;
    const s = Math.max(0, ns[i*K+q]), rest = 12 + (1 - s) * 70, k = 0.02 + 0.09 * s;
    const ddx = pos[2*j] - pos[2*i], ddy = pos[2*j+1] - pos[2*i+1];
    const d = Math.sqrt(ddx * ddx + ddy * ddy) || 0.01, f = (d - rest) * k;
    fx[i] += ddx / d * f; fy[i] += ddy / d * f;
  }
  for(let i = 0; i < n; i++){                       // a little gravity, then move
    fx[i] -= pos[2*i] * 0.0035; fy[i] -= pos[2*i+1] * 0.0035;
    vel[2*i]   = (vel[2*i]   + fx[i] * alpha) * 0.78;
    vel[2*i+1] = (vel[2*i+1] + fy[i] * alpha) * 0.78;
    pos[2*i] += vel[2*i]; pos[2*i+1] += vel[2*i+1];
  }
}
// GX-COMPUTE-END

const G = {state:"idle", n:0, ids:[], nodes:[], V:null, nb:null, ns:null, P:null, K:6, alpha:1, tick:0,
  tf:{k:1, x:0, y:0}, moved:false, hover:-1, tmin:0, tmax:0, tcut:0, playing:false, last:0,
  mode:"wing", links:false, health:true, solo:null, since:0, privacy:false, wired:false, dataCount:-1, raf:0, drag:null, rank:new Map(), focus:-1, refit:false, wiredW:false, wiredR:false, idxOf:null, open:new Set()};

function gxRequest(){ if(!G.raf) G.raf = requestAnimationFrame(gxFrame); }
function gxMsg(t){ const m = document.getElementById("gxMsg"); if(m) m.textContent = t || ""; }
// Hovering an entry in the list rings its dot on the map: where does this one live?
// A "target" is the one drawer the user asked to see: it gets a colour nothing else uses, its closest neighbours stay
// lit with lines to them, and everything else drops to a whisper. It ends with Esc, "fit", or a click on empty map.
function gxSetTarget(i, move){
  const d = DATA.drawers[i]; if(!d || !G.idxOf || !G.idxOf.has(d.id) || !G.P) return;
  const gi = G.idxOf.get(d.id); G.target = gi; G.targetId = d.id; G.focus = gi;
  try{ G.tchain = gxChainFor(gi); }catch(e){ G.tchain = null; }
  if(move){ const x = G.P.pos[2*gi], y = G.P.pos[2*gi+1], k = Math.max(G.tf.k, 2.4);
    G.tf.k = k; G.tf.x = (G.w || 600) / 2 - x * k; G.tf.y = (G.h || 500) / 2 - y * k; G.moved = true; }
  gxRequest();
}
function gxClearTarget(){ if(G.target >= 0 || G.focus >= 0){ G.target = -1; G.targetId = null; G.focus = -1; G.tchain = null; gxRequest(); } }
function gxLocate(i){ gxSetTarget(i, true); }       // "Show on map": select, centre and zoom
function gxFocus(i){ const d = DATA.drawers[i]; G.focus = (d && G.idxOf && G.idxOf.has(d.id)) ? G.idxOf.get(d.id) : -1; gxRequest(); }

async function gxLoad(){
  if(G.state === "loading" || G.state === "layout") return;
  G.state = "loading"; G.dataCount = DATA.count;
  try{
    gxMsg("Fetching each drawer's stored embedding…");
    const r = await fetch("/api/galaxy"), j = await r.json();
    if(!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    const byId = new Map(DATA.drawers.map(d => [d.id, d]));
    G.ids = j.ids; G.n = j.ids.length;
    G.nodes = j.ids.map(id => byId.get(id) || {id, wing:"(unknown)", room:"", filed_at:"", content:"", bytes:0, added_by:""});
    gxMsg("Decoding " + G.n.toLocaleString() + " vectors…");
    G.V = gxDecode(j.vectors, G.n, j.dim);
    const kn = await gxKnn(G.V, G.n, j.dim, G.K, p => gxMsg("Finding each drawer's nearest neighbours… " + Math.round(p * 100) + "%"));
    G.nb = kn.nb; G.ns = kn.ns;
    G.P = gxInit(G.V, G.n, j.dim); G.alpha = 1; G.tick = 0; G.moved = false;
    gxPrep(); gxLegend();
    G.state = "layout"; gxMsg("Settling the galaxy…");
    gxRequest();
  }catch(e){
    G.state = "idle"; gxMsg("The Galaxy could not be built: " + (e && e.message ? e.message : e));
  }
}
function gxPrep(){                                   // per-drawer display data
  G.idxOf = new Map(G.ids.map((id, i) => [id, i]));
  const wc = new Map(), ac = new Map();
  for(const d of G.nodes){ wc.set(d.wing, (wc.get(d.wing) || 0) + 1); { const ak = authorKey(d.added_by); ac.set(ak, (ac.get(ak) || 0) + 1); } }
  G.rank = new Map([...wc.entries()].sort((a, b) => b[1] - a[1]).map((e, i) => [e[0], i]));
  G.arank = new Map([...ac.entries()].sort((a, b) => b[1] - a[1]).map((e, i) => [e[0], i]));
  G.t = new Float64Array(G.n); G.r = new Float32Array(G.n); G.mod = new Float64Array(G.n); G.rew = new Float64Array(G.n);
  G.tail = new Uint8Array(G.n);                     // small wings get a ring so they cannot vanish next to the big two
  G.nodes.forEach((d, i) => { G.tail[i] = (wc.get(d.wing) < 20) ? 1 : 0; });
  let lo = Infinity, hi = -Infinity;
  G.nodes.forEach((d, i) => {
    const t = Date.parse(d.filed_at); G.t[i] = isNaN(t) ? 0 : t;
    const lm = Date.parse((d.meta || {}).last_modified);        // edited = changed well after it was filed
    G.mod[i] = (!isNaN(lm) && lm > G.t[i] + 120000) ? lm : 0;       // modified: includes moves between wings/rooms
    { const st = ((DATA.revsecrets || {}).stats || {})[(d.meta && d.meta.parent_drawer_id) || d.id], rt = st ? Date.parse(st[1]) : NaN;
      G.rew[i] = isNaN(rt) ? 0 : rt; }                              // rewritten: the revision log says the words changed
    if(G.t[i]){ lo = Math.min(lo, G.t[i]); hi = Math.max(hi, G.t[i]); }
    if(G.rew[i]) hi = Math.max(hi, G.rew[i]);         // an edit after the newest filing still belongs on the timeline
    G.r[i] = Math.max(1.8, Math.min(5.2, 1.4 + Math.sqrt(d.bytes || 0) / 22));
  });
  G.tmin = isFinite(lo) ? lo : 0; G.tmax = isFinite(hi) ? hi : 0; G.tcut = G.tmax;
  G.secret = new Set(((DATA.secrets || {}).ids) || []); G.cf = null; G.target = -1; G.targetId = null; G.tchain = null; G.chain = null; G.sup = null;
  {                                                     // staleness: filed or REWRITTEN (revision log), never last_modified
    const rst = (DATA.revsecrets || {}).stats || {}, day = 86400000, now = Date.now();
    G.touch = new Float64Array(G.n); G.nrev = new Uint16Array(G.n); G.staleN = 0; G.haveRev = Object.keys(rst).length > 0;
    G.nodes.forEach((d, i) => {
      const st = rst[(d.meta && d.meta.parent_drawer_id) || d.id], lr = st ? Date.parse(st[1]) : NaN;
      G.nrev[i] = st ? st[0] : 0;
      G.touch[i] = Math.max(G.t[i] || 0, isNaN(lr) ? 0 : lr);
      if(G.touch[i] && (now - G.touch[i]) / day > 180) G.staleN++;
    });
  }
  gxColors();
}
function gxColors(){
  const day = 86400000, inWing = (G.mode === "wing" && sel.wing) ? sel.wing : null;
  G.colWing = inWing;
  if(inWing){                                          // a wing is picked: colour ITS rooms, grey the rest
    const rc = new Map(); G.nodes.forEach(d => { if(d.wing === inWing) rc.set(d.room, (rc.get(d.room) || 0) + 1); });
    G.rrank = new Map([...rc.entries()].sort((a, b) => b[1] - a[1]).map((e, i) => [e[0], i]));
  }
  G.col = G.nodes.map((d, i) => {
    if(inWing) return d.wing === inWing ? `hsl(${Math.round((G.rrank.get(d.room) * 137.508) % 360)},78%,62%)` : "hsl(215,12%,38%)";
    if(G.mode === "age"){
      const a = Math.max(0, (G.tmax - G.t[i]) / day), f = Math.exp(-a / 120);
      return `hsl(${Math.round(215 - 180 * f)},85%,${Math.round(50 + 12 * f)}%)`;
    }
    if(G.mode === "author"){
      if(!d.added_by) return "hsl(215,10%,55%)";
      return `hsl(${Math.round((G.arank.get(authorKey(d.added_by)) * 137.508) % 360)},75%,62%)`;
    }
    return `hsl(${Math.round((G.rank.get(d.wing) * 137.508) % 360)},72%,62%)`;
  });
}
function gxYmd(ms){ const d = new Date(ms); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
function gxFit(smooth){
  const n = G.n, pos = G.P.pos, xs = [], ys = [];
  for(let i = 0; i < n; i++){ xs.push(pos[2*i]); ys.push(pos[2*i+1]); }
  xs.sort((a, b) => a - b); ys.sort((a, b) => a - b);
  const lo = Math.floor(n * 0.015), hi = Math.min(n - 1, Math.ceil(n * 0.985)),
        x0 = xs[lo], x1 = xs[hi], y0 = ys[lo], y1 = ys[hi], W = G.w || 800, H = G.h || 500;
  const k = Math.min((W - 90) / Math.max(1, x1 - x0), (H - 90) / Math.max(1, y1 - y0)),
        x = W / 2 - (x0 + x1) / 2 * k, y = H / 2 - (y0 + y1) / 2 * k;
  if(smooth){ G.tf.k += (k - G.tf.k) * .12; G.tf.x += (x - G.tf.x) * .12; G.tf.y += (y - G.tf.y) * .12; }
  else { G.tf = {k, x, y}; }
}
function gxMatches(){
  const m = new Uint8Array(G.n);
  for(let i = 0; i < G.n; i++){
    const d = G.nodes[i];
    m[i] = (matches(d) && (!G.solo || d.wing === G.solo)) ? 1 : 0;
  }
  return m;
}
function gxFrame(){
  G.raf = 0;
  const cv = document.getElementById("gxc");
  if(!cv || !cv.isConnected || !G.P) return;        // left the view: the loop ends, and restarts on return
  const now = performance.now();
  if(G.refit && cv.clientWidth){ G.w = cv.clientWidth; G.h = cv.clientHeight; if(!G.moved) gxFit(false); G.refit = false; }
  if(G.state === "layout"){
    for(let s = 0; s < 2 && G.alpha > 0.03; s++){ gxStep(G.P, G.nb, G.ns, G.n, G.K, G.alpha); G.alpha *= 0.992; G.tick++; }
    if(!G.moved) gxFit(true);
    if(G.alpha <= 0.03){ G.state = "ready"; gxMsg(""); if(!G.moved) gxFit(false); gxConflictsInit(); }
  }
  if(G.playing){
    const span = Math.max(1, G.tmax - G.tmin);
    G.tcut += span * (now - (G.last || now)) / 15000;
    if(G.tcut >= G.tmax){ G.tcut = G.tmax; G.playing = false; const b = document.getElementById("gxPlay"); if(b) b.textContent = "▶ Replay"; }
    const s = document.getElementById("gxSlide"); if(s) s.value = String(Math.round((G.tcut - G.tmin) / span * 1000));
  }
  G.last = now;
  gxDraw(cv, now);
  const pulsing = G.health && G.secret && G.secret.size;
  if(G.state === "layout" || G.playing || pulsing || G.focus >= 0 || G.target >= 0) gxRequest();
}
function gxDraw(cv, now){
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  if(cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)){ cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  G.w = W; G.h = H;
  const c = cv.getContext("2d"); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, W, H);
  const {k, x: tx, y: ty} = G.tf, n = G.n, pos = G.P.pos, match = G.match || gxMatches(), anyFilter = !!(query || sel.wing || G.solo);
  const vis = new Uint8Array(n), SX = new Float32Array(n), SY = new Float32Array(n);
  const cut = G.tcut, timed = G.tmax > G.tmin && cut < G.tmax;
  let shown = 0, hits = 0, nNew = 0, nEdit = 0, nBoth = 0;
  const since = G.since, sinceOn = since > 0;
  const kind = new Uint8Array(n);                   // 1 = new since the chosen date, 2 = edited since
  for(let i = 0; i < n; i++){
    SX[i] = pos[2*i] * k + tx; SY[i] = pos[2*i+1] * k + ty;
    vis[i] = (timed && G.t[i] > cut) ? 0 : 1;
    if(vis[i]){ shown++; if(match[i]) hits++; }
    if(sinceOn && vis[i]){
      const isN = G.t[i] >= since, isE = G.rew[i] >= since;   // 'edited' counts every drawer edited in the window, new or not,
      if(isN){ kind[i] = 1; nNew++; } else if(isE){ kind[i] = 2; }   // so widening the window can never lower it
      if(isE) nEdit++;
      if(isN && isE) nBoth++;
    }
  }
  if(G.links){
    c.lineWidth = 0.7; c.strokeStyle = "rgba(110,150,255,.11)"; c.beginPath();
    for(let i = 0; i < n; i++){
      if(!vis[i]) continue;
      for(let q = 0; q < 3; q++){
        const j = G.nb[i*G.K+q]; if(j < 0 || !vis[j] || j < i) continue;
        c.moveTo(SX[i], SY[i]); c.lineTo(SX[j], SY[j]);
      }
    }
    c.stroke();
  }
  const rs = Math.min(2.4, Math.max(0.9, Math.sqrt(k) * 1.1)), nowMs = Date.now();
  if(G.fresh && !G.sup) gxSupCompute();
  const tg = (G.target >= 0 && G.target < n) ? G.target : -1, isNbr = new Uint8Array(n);
  if(tg >= 0) for(let q = 0; q < G.K; q++){ const j = G.nb[tg*G.K+q]; if(j >= 0) isNbr[j] = 1; }
  if(tg >= 0 && G.tchain) G.tchain.path.forEach(s => { isNbr[s.gi] = 1; });
  const span = Math.max(1, G.tmax - G.tmin);
  for(let i = 0; i < n; i++){
    if(!vis[i]) continue;
    const sx = SX[i], sy = SY[i];
    if(sx < -10 || sy < -10 || sx > W + 10 || sy > H + 10) continue;
    const dim = anyFilter && !match[i];
    // Brightness means "recent" in wing/author colour; in age colour the colour already says it, so brightness stays flat.
    let al = (G.mode === "age") ? 0.95 : 0.55 + 0.45 * Math.exp(-Math.max(0, (G.tmax - G.t[i])) / (90 * 86400000));
    if(G.fresh && G.touch) al = 0.1 + 0.9 * Math.exp(-Math.max(0, (nowMs - G.touch[i]) / 86400000) / 120);   // staleness overlay
    if(G.fresh && G.sup && G.sup[i]) al = Math.min(al, 0.16);            // a newer drawer replaces this one: not current, however young
    if(dim) al = (G.halo && G.halo[i]) ? 0.3 : 0.07;
    if(sinceOn && !kind[i]) al = Math.min(al, 0.1);
    if(tg >= 0) al = (i === tg) ? 1 : (isNbr[i] ? 0.95 : Math.min(al, 0.05));   // the shown drawer and its neighbours; the rest whispers
    c.globalAlpha = al;
    c.fillStyle = G.col[i];
    const rr = G.r[i] * rs * (G.tail[i] ? 1.35 : 1);
    c.beginPath(); c.arc(sx, sy, rr, 0, 6.2832); c.fill();
    if(G.tail[i] && !dim){ c.globalAlpha = Math.min(1, al + 0.3); c.strokeStyle = "rgba(255,255,255,.8)"; c.lineWidth = 0.9;
      c.beginPath(); c.arc(sx, sy, rr + 1.6, 0, 6.2832); c.stroke(); c.globalAlpha = al; }
    if(sinceOn && kind[i] && (tg < 0 || i === tg || isNbr[i])){  // changed since the chosen date
      c.globalAlpha = 0.95; c.lineWidth = 1.3; c.strokeStyle = kind[i] === 1 ? "#ffffff" : "#f59e0b";
      c.beginPath(); c.arc(sx, sy, G.r[i] * rs + 2.5, 0, 6.2832); c.stroke();
    }
    if(G.playing && G.rew[i] && cut >= G.rew[i] && cut - G.rew[i] < span * 0.03){   // a drawer being REWRITTEN
      const a = 1 - (cut - G.rew[i]) / (span * 0.03);
      c.globalAlpha = 0.9 * a; c.strokeStyle = "#f59e0b"; c.lineWidth = 1.6;
      c.beginPath(); c.arc(sx, sy, G.r[i] * rs + 3 + 11 * (1 - a), 0, 6.2832); c.stroke();
    }
    if(G.playing && G.t[i] && cut - G.t[i] < span * 0.025){        // a drawer being born
      const a = 1 - (cut - G.t[i]) / (span * 0.025);
      c.globalAlpha = 0.5 * a; c.strokeStyle = "#ffffff"; c.lineWidth = 1.2;
      c.beginPath(); c.arc(sx, sy, G.r[i] * rs + 3 + 9 * (1 - a), 0, 6.2832); c.stroke();
    }
  }
  c.globalAlpha = 1;
  if(G.health && G.secret && G.secret.size){
    const pulse = 0.5 + 0.5 * Math.sin(now / 260);
    c.lineWidth = 1.8; c.strokeStyle = `rgba(239,68,68,${0.55 + 0.45 * pulse})`;
    for(const id of G.secret){
      const i = G.ids.indexOf(id); if(i < 0 || !vis[i]) continue;
      c.beginPath(); c.arc(SX[i], SY[i], G.r[i] * rs + 5 + 4 * pulse, 0, 6.2832); c.stroke();
    }
  }
  if(tg >= 0){                                       // the drawer the user asked to see
    const pulse = 0.5 + 0.5 * Math.sin(now / 260), PK = "#ff3df0";
    c.globalAlpha = 0.9; c.strokeStyle = PK; c.lineWidth = 1.6; c.beginPath();
    for(let q = 0; q < G.K; q++){ const j = G.nb[tg*G.K+q]; if(j >= 0){ c.moveTo(SX[tg], SY[tg]); c.lineTo(SX[j], SY[j]); } }
    c.stroke();
    c.lineWidth = 1.4; c.globalAlpha = 0.85;                       // a ring on each neighbour: these are its "linked items"
    for(let q = 0; q < G.K; q++){ const j = G.nb[tg*G.K+q]; if(j >= 0){ c.beginPath(); c.arc(SX[j], SY[j], G.r[j] * rs + 3, 0, 6.2832); c.stroke(); } }
    c.globalAlpha = 0.22 + 0.18 * pulse; c.fillStyle = PK; c.beginPath(); c.arc(SX[tg], SY[tg], G.r[tg] * rs + 16 + 6 * pulse, 0, 6.2832); c.fill();
    c.globalAlpha = 1; c.fillStyle = PK; c.beginPath(); c.arc(SX[tg], SY[tg], Math.max(5, G.r[tg] * rs * 1.9), 0, 6.2832); c.fill();
    c.strokeStyle = "#ffffff"; c.lineWidth = 2; c.beginPath(); c.arc(SX[tg], SY[tg], Math.max(5, G.r[tg] * rs * 1.9) + 2, 0, 6.2832); c.stroke();
  }
  if(tg >= 0 && G.tchain && G.tchain.path.length > 1){      // the supersede chain: older -> newer, solid when the drawer says so
    const TL = "#22e0d0", pth = G.tchain.path;
    for(let i = 0; i < pth.length - 1; i++){
      const A = pth[i].gi, B = pth[i+1].gi, solid = G.tchain.kinds[i] === "says";
      c.globalAlpha = 0.95; c.strokeStyle = TL; c.fillStyle = TL; c.lineWidth = solid ? 2.4 : 1.5; c.setLineDash(solid ? [] : [5, 4]);
      c.beginPath(); c.moveTo(SX[A], SY[A]); c.lineTo(SX[B], SY[B]); c.stroke(); c.setLineDash([]);
      const ang = Math.atan2(SY[B] - SY[A], SX[B] - SX[A]), rb = Math.max(5, G.r[B] * rs * 1.9) + 5, ax = SX[B] - Math.cos(ang) * rb, ay = SY[B] - Math.sin(ang) * rb;
      c.beginPath(); c.moveTo(ax, ay); c.lineTo(ax - 10 * Math.cos(ang - .42), ay - 10 * Math.sin(ang - .42)); c.lineTo(ax - 10 * Math.cos(ang + .42), ay - 10 * Math.sin(ang + .42)); c.closePath(); c.fill();
    }
    c.font = "600 10px system-ui, sans-serif"; c.textAlign = "center";
    for(const s of pth){
      c.globalAlpha = 1; c.strokeStyle = TL; c.lineWidth = 1.8; c.beginPath(); c.arc(SX[s.gi], SY[s.gi], G.r[s.gi] * rs + 5, 0, 6.2832); c.stroke();
      if(!G.privacy && s.day){ const lab = s.day.slice(5); c.lineWidth = 3; c.strokeStyle = "rgba(5,8,20,.9)"; c.strokeText(lab, SX[s.gi], SY[s.gi] - G.r[s.gi] * rs - 9); c.fillStyle = "#c9fff8"; c.fillText(lab, SX[s.gi], SY[s.gi] - G.r[s.gi] * rs - 9); }
    }
    c.font = "600 12px system-ui, sans-serif";
  }
  if(G.hover >= 0 && vis[G.hover]){
    const h = G.hover;
    c.strokeStyle = "rgba(255,255,255,.55)"; c.lineWidth = 1;
    c.beginPath();
    for(let q = 0; q < G.K; q++){ const j = G.nb[h*G.K+q]; if(j >= 0 && vis[j]){ c.moveTo(SX[h], SY[h]); c.lineTo(SX[j], SY[j]); } }
    c.stroke();
    c.strokeStyle = "#fff"; c.lineWidth = 1.8; c.beginPath(); c.arc(SX[h], SY[h], G.r[h] * rs + 3, 0, 6.2832); c.stroke();
  }
  if(G.focus >= 0 && vis[G.focus]){                 // the entry being hovered in the list
    const f = G.focus, pulse = 0.5 + 0.5 * Math.sin(now / 200);
    c.globalAlpha = 1; c.fillStyle = "rgba(255,255,255,.16)"; c.beginPath(); c.arc(SX[f], SY[f], G.r[f] * rs + 12, 0, 6.2832); c.fill();
    c.strokeStyle = "#ffffff"; c.lineWidth = 2; c.beginPath(); c.arc(SX[f], SY[f], G.r[f] * rs + 5 + 3 * pulse, 0, 6.2832); c.stroke();
  }
  G.labelRects = [];
  if(!G.privacy){                                     // wing names at the centre of their dots, in every colour mode
    const acc = new Map();
    for(let i = 0; i < n; i++){
      if(!vis[i]) continue;
      const w = G.nodes[i].wing; let a = acc.get(w); if(!a){ a = {x:0, y:0, n:0}; acc.set(w, a); }
      a.x += SX[i]; a.y += SY[i]; a.n++;
    }
    c.font = "600 12px system-ui, sans-serif"; c.textAlign = "center"; c.lineJoin = "round";
    // Label boxes run from y-12 to y+4 (baseline y). M is breathing room between boxes.
    const placed = [], M = 4;
    // The legend and the key sit on top of the map, so a label must not land under them either.
    const cr = cv.getBoundingClientRect();
    ["gxLegend", "gxKey", "gxChain"].forEach(id => {
      const el = document.getElementById(id);
      if(el && el.style.display !== "none"){ const r = el.getBoundingClientRect();
        if(r.width) placed.push({l: r.left - cr.left - M, r: r.right - cr.left + M, t: r.top - cr.top - M, b: r.bottom - cr.top + M}); }
    });
    const free = (l, r, t, b) => l >= 4 && r <= W - 4 && t >= 4 && b <= H - 4 && !placed.some(p => l < p.r && r > p.l && t < p.b && b > p.t);
    const spots = [[0, 0]];                         // the cluster's centre first, then the nearest free spot around it
    for(const d of [18, 34, 54, 80]) for(const [ux, uy] of [[0, -1], [0, 1], [1, 0], [-1, 0], [.7, -.7], [-.7, -.7], [.7, .7], [-.7, .7]]) spots.push([ux * d, uy * d]);
    let shownL = 0, totalL = 0;
    for(const [w, a] of [...acc.entries()].sort((p, q) => q[1].n - p[1].n)){      // biggest wing gets its centre first
      if(a.n < 1 || (G.solo && w !== G.solo)) continue;
      const x0 = a.x / a.n, y0 = a.y / a.n; if(x0 < 0 || y0 < 0 || x0 > W || y0 > H) continue;
      totalL++;
      const tw = c.measureText(w).width + 8, hw = tw / 2;
      let pick = null;
      for(const [dx, dy] of spots){ const x = x0 + dx, y = y0 + dy; if(free(x - hw - M, x + hw + M, y - 12 - M, y + 4 + M)){ pick = {x, y, dx, dy}; break; } }
      if(!pick) continue;
      placed.push({l: pick.x - hw - M, r: pick.x + hw + M, t: pick.y - 12 - M, b: pick.y + 4 + M}); shownL++;
      if(pick.dx || pick.dy){                       // moved off its centre: a thin leader line shows where it belongs
        c.strokeStyle = "rgba(255,255,255,.35)"; c.lineWidth = 1; c.beginPath(); c.moveTo(x0, y0); c.lineTo(pick.x, pick.y - 4); c.stroke();
        c.fillStyle = "rgba(255,255,255,.55)"; c.beginPath(); c.arc(x0, y0, 2, 0, 6.2832); c.fill();
      }
      c.globalAlpha = 1; c.fillStyle = "rgba(5,8,20,.62)"; c.fillRect(pick.x - hw + 1, pick.y - 12, tw - 2, 16);     // a pill, so rings under a label cannot hide its letters
      c.lineWidth = 3.5; c.strokeStyle = "rgba(5,8,20,.85)"; c.strokeText(w, pick.x, pick.y);
      c.fillStyle = "rgba(255,255,255,.92)"; c.fillText(w, pick.x, pick.y);
      (G.labelRects || (G.labelRects = [])).push({w, l: pick.x - hw, r: pick.x + hw, t: pick.y - 12, b: pick.y + 4});
    }
    G.labelStat = {shown: shownL, total: totalL};
  }
  const st = document.getElementById("gxStat");
  if(st) st.textContent = (sinceOn ? nNew.toLocaleString() + " new · " + nEdit.toLocaleString() + " rewritten" + (nBoth ? " (" + nBoth + " also new)" : "") + " since " + gxYmd(since) + " · " : "")
    + (anyFilter ? hits.toLocaleString() + " of " : "") + shown.toLocaleString() + " drawers" + (G.state === "layout" ? " · settling…" : "") + (G.privacy ? " · privacy on" : "");
  const dt = document.getElementById("gxDate");
  if(dt) dt.textContent = (G.tmax > G.tmin) ? gxYmd(G.tcut) : "";
  const nb = document.getElementById("gxNow"); if(nb) nb.style.display = (G.tmax > G.tmin && G.tcut < G.tmax) ? "" : "none";
  gxKey(sinceOn); gxChainPanel();
  G.sx = SX; G.sy = SY; G.vis = vis;
}
function gxKey(sinceOn){
  const el = document.getElementById("gxKey"); if(!el) return;
  const rows = [];
  const ring = (col, txt, dashed) => rows.push(`<div><i class="rg" style="border-color:${col}${dashed ? ";border-style:dotted" : ""}"></i>${txt}</div>`);
  if(G.tail && G.tail.some(v => v)) ring("rgba(255,255,255,.8)", "ringed, larger dot = small wing (under 20 drawers)");
  if(sinceOn){ ring("#ffffff", "white ring = filed since the date"); ring("#f59e0b", "amber ring = text rewritten since (and not new)"); }
  if(G.health && G.secret && G.secret.size) ring("#ef4444", "red pulse = text shaped like a credential");
  if(G.playing) ring("#f59e0b", "pulse = a drawer being rewritten", true);
  if(G.target >= 0 && G.tchain && G.tchain.path.length > 1) ring("#22e0d0", "teal arrows = supersede chain, oldest to newest (solid: the newer drawer says it replaces the older; dashed: inferred)", false);
  if(G.target >= 0) ring("#ff3df0", "pink = the drawer you asked to see; lines and rings = its closest neighbours (Esc, fit, or a click on empty map to leave)", false);
  if(G.fresh) ring("rgba(255,255,255,.28)", "faded = not filed or rewritten lately (" + G.staleN + " of " + G.n + " untouched 180+ days); a drawer that a newer one replaces fades further" + (G.haveRev ? "" : "; no revision data"), false);
  const html = (G.privacy || !rows.length) ? "" : rows.join("");
  if(el.dataset.k === html && el.dataset.s === "1") return;      // per-element cache: the canvas is rebuilt when the view changes
  el.dataset.k = html; el.dataset.s = "1"; el.innerHTML = html; el.style.display = html ? "" : "none";
}
function gxLegend(){
  const el = document.getElementById("gxLegend"); if(!el) return;
  if(G.privacy){ el.style.display = "none"; return; }
  if(G.mode === "age"){
    el.style.display = "";
    el.innerHTML = `<div style="color:var(--fg);cursor:default;font-weight:600">Colour = age (filed)</div>
      <div style="cursor:default;width:150px;height:8px;border-radius:4px;margin:4px 0 2px;background:linear-gradient(90deg,hsl(215,85%,50%),hsl(125,85%,56%),hsl(35,85%,62%))"></div>
      <div style="cursor:default;display:flex;justify-content:space-between"><span>${G.tmin ? esc(gxYmd(G.tmin)) : ""}</span><span>${G.tmax ? esc(gxYmd(G.tmax)) : ""}</span></div>`;
    return;
  }
  if(G.mode === "author"){
    el.style.display = "";
    const ents = [...G.arank.entries()].filter(e => e[0] !== "");
    el.innerHTML = `<div style="color:var(--fg);cursor:default;font-weight:600">Colour = author</div>` + ents.slice(0, 16).map(([a, k]) =>
      `<div style="cursor:default"><i style="background:hsl(${Math.round((k * 137.508) % 360)},75%,62%)"></i>${esc(a)}</div>`).join("")
      + (ents.length > 16 ? `<div style="cursor:default">… ${ents.length - 16} more</div>` : "")
      + `<div style="cursor:default"><i style="background:hsl(215,10%,55%)"></i>no author recorded</div>`;
    return;
  }
  if(G.mode !== "wing" || !G.rank.size){ el.style.display = "none"; return; }
  el.style.display = "";
  if(G.colWing){
    const rooms = [...G.rrank.entries()];
    el.innerHTML = `<div style="color:var(--fg);cursor:default;font-weight:600">${esc(G.colWing)} · rooms</div>` + rooms.slice(0, 22).map(([r, k]) =>
      `<div style="cursor:default"><i style="background:hsl(${Math.round((k * 137.508) % 360)},78%,62%)"></i>${esc(r)}</div>`).join("");
    return;
  }
  el.innerHTML = [...G.rank.entries()].map(([w, r]) => `<div data-w="${esc(w)}" class="${G.solo && G.solo !== w ? "off" : ""}">
      <i style="background:hsl(${Math.round((r * 137.508) % 360)},72%,62%)"></i>${esc(w)}</div>`).join("");
  el.querySelectorAll("[data-w]").forEach(d => d.onclick = () => {
    G.solo = (G.solo === d.dataset.w) ? null : d.dataset.w; galaxyRedraw();
  });
}
function galaxyRedraw(){
  if(G.P && ((G.mode === "wing" && sel.wing) || null) !== (G.colWing || null)) gxColors();   // picking a wing recolours by room
  G.match = G.P ? gxMatches() : null; G.halo = null;
  if(G.match && (query || sel.wing || G.solo)){            // a faint halo on the neighbours of every hit
    const h = new Uint8Array(G.n);
    for(let i = 0; i < G.n; i++) if(G.match[i]) for(let q = 0; q < G.K; q++){ const j = G.nb[i*G.K+q]; if(j >= 0 && !G.match[j]) h[j] = 1; }
    G.halo = h;
  }
  gxLegend(); gxRequest();
}
function gxPick(mx, my){
  if(!G.sx || G.privacy) return -1;
  let best = -1, bd = 12 * 12;
  for(let i = 0; i < G.n; i++){
    if(!G.vis[i]) continue;
    if((query || sel.wing || G.solo) && G.match && !G.match[i]) continue;
    const dx = G.sx[i] - mx, dy = G.sy[i] - my, d2 = dx * dx + dy * dy;
    if(d2 < bd){ bd = d2; best = i; }
  }
  return best;
}
function gxBuild(host, compact){
  if(!document.getElementById("gxc")){
    host.innerHTML = `<div class="galaxy${compact ? " gx-compact" : ""}">
      <div class="gx-bar">
        <label>Colour <select id="gxMode"><option value="wing">wing</option><option value="age">age</option><option value="author">author</option></select></label>
        <label class="gx-opt"><input type="checkbox" id="gxLinks"> links</label>
        <label class="gx-opt"><input type="checkbox" id="gxHealth" checked> health</label>
        <label class="gx-opt">Changed since <input type="date" id="gxSince"></label>
        <button data-since="1">24 h</button><button data-since="7">7 d</button><button data-since="30">30 d</button><button data-since="0">clear</button>
        <button id="gxPlay" class="gx-opt">▶ Play history</button>
        <input type="range" id="gxSlide" class="gx-opt" min="0" max="1000" value="1000">
        <span class="gx-date gx-opt" id="gxDate"></span><button id="gxNow" class="gx-opt" style="display:none" title="Leave the replay and show the palace as it is today">⟲ present</button>
        <button id="gxPriv" title="Hide titles, labels and tooltips (key: P)">◐ privacy</button><button id="gxConf" style="display:none">≠ conflicts</button><button id="gxFresh" title="Fade drawers nobody has filed or rewritten lately. A rewrite is read from MemPalace's revision log; moving a drawer between wings does not count.">◔ staleness</button>
        <button id="gxFitBtn" title="Show the whole galaxy">fit</button>
        <button id="gxExpand" class="gx-co" title="Open the full Galaxy">⤢ expand</button>
        <button id="gxListBtn" style="display:none;max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></button><span id="gxStat" style="margin-left:auto;color:var(--fg-subtle)"></span>
      </div>
      <div class="gx-wrap"><canvas id="gxc"></canvas><div class="gx-legend" id="gxLegend"></div><div class="gx-key" id="gxKey" style="display:none"></div><div class="gx-chain" id="gxChain" style="display:none"></div>
        <div class="gx-tip" id="gxTip" hidden></div><div class="gx-msg" id="gxMsg"></div>
        <div class="gx-hint">drag to pan · scroll to zoom · click a dot to open it · type to search · P = privacy (map only: the sidebar keeps its names) · L = list a pile</div></div></div>`;
    gxWire(); G.refit = true; G.moved = false;
  }
  const ex = document.getElementById("gxExpand"); if(ex) ex.onclick = () => { view = "galaxy"; render(); };
  const fb = document.getElementById("gxFitBtn"); if(fb) fb.onclick = () => { G.moved = false; G.target = -1; G.targetId = null; G.focus = -1; G.tchain = null; if(G.P) gxFit(false); gxRequest(); };
  document.getElementById("gxMode").value = G.mode;
  document.getElementById("gxLinks").checked = G.links;
  document.getElementById("gxHealth").checked = G.health;
  if(G.state === "idle" || (G.state === "ready" && G.dataCount !== DATA.count)){ G.state = "idle"; gxLoad(); }
  else if(G.state === "loading"){ gxMsg("Building the galaxy…"); }
  else { gxMsg(""); galaxyRedraw(); }
}
function renderGalaxy(){ gxBuild(document.getElementById("content"), false); }
function gxWire(){
  const cv = document.getElementById("gxc"), tip = document.getElementById("gxTip");
  document.getElementById("gxMode").onchange = e => { G.mode = e.target.value; gxColors(); gxLegend(); gxRequest(); };
  document.getElementById("gxLinks").onchange = e => { G.links = e.target.checked; gxRequest(); };
  document.getElementById("gxHealth").onchange = e => { G.health = e.target.checked; gxRequest(); };
  const sinceEl = document.getElementById("gxSince");
  const setSince = ms => { G.since = ms; sinceEl.value = ms ? new Date(ms - new Date(ms).getTimezoneOffset() * 60000).toISOString().slice(0, 10) : ""; gxRequest(); };
  sinceEl.onchange = () => { G.since = sinceEl.value ? Date.parse(sinceEl.value + "T00:00:00") : 0; gxRequest(); };
  document.querySelectorAll("[data-since]").forEach(b => b.onclick = () => { const dd = +b.dataset.since; setSince(dd ? Date.now() - dd * 86400000 : 0);
    if(!dd && G.tmax > G.tmin){ G.tcut = G.tmax; G.playing = false; const sl = document.getElementById("gxSlide"); if(sl) sl.value = "1000"; const pb = document.getElementById("gxPlay"); if(pb) pb.textContent = "▶ Play history"; } });
  const fb = document.getElementById("gxFresh");
  if(fb){ fb.style.borderColor = G.fresh ? "var(--brand)" : ""; fb.onclick = () => { G.fresh = !G.fresh; fb.style.borderColor = G.fresh ? "var(--brand)" : ""; gxRequest(); }; }
  const cfb = document.getElementById("gxConf"); if(cfb){ cfb.onclick = () => conflictsSheet(); gxConfLabel(); }
  const lbtn = document.getElementById("gxListBtn"); if(lbtn) lbtn.onclick = gxListCluster;
  const setPriv = on => { G.privacy = on; tip.hidden = true; G.hover = -1; { const lb2 = document.getElementById("gxListBtn"); if(on && lb2) lb2.style.display = "none"; else G.lastRoom = null; } document.getElementById("gxPriv").style.borderColor = on ? "var(--brand)" : ""; gxLegend(); gxRequest(); };
  document.getElementById("gxPriv").onclick = () => setPriv(!G.privacy);
  if(!G.wired){ G.wired = true;
    document.addEventListener("keydown", e => {
      if(!document.getElementById("gxc") || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if(e.key === "p" || e.key === "P") setPriv(!G.privacy);
      else if((e.key === "l" || e.key === "L") && (G.cluster || G.lastRoom) && !G.privacy) gxListCluster();
    });
    const u = new URLSearchParams(location.search).get("since"); if(u && !isNaN(Date.parse(u))) setSince(Date.parse(u + "T00:00:00")); }
  const play = document.getElementById("gxPlay"), slide = document.getElementById("gxSlide");
  play.onclick = () => {
    if(G.state !== "ready" && G.state !== "layout") return;
    if(G.playing){ G.playing = false; play.textContent = "▶ Play history"; return; }
    if(G.tcut >= G.tmax) G.tcut = G.tmin;
    G.playing = true; G.last = 0; play.textContent = "❚❚ Pause"; gxRequest();
  };
  slide.oninput = () => { G.playing = false; play.textContent = "▶ Play history";
    G.tcut = G.tmin + (G.tmax - G.tmin) * (+slide.value) / 1000; gxRequest(); };
  const nowb = document.getElementById("gxNow");
  if(nowb) nowb.onclick = () => { G.tcut = G.tmax; G.playing = false; play.textContent = "▶ Play history"; slide.value = "1000"; gxRequest(); };
  cv.onmousedown = e => { G.drag = {x: e.clientX, y: e.clientY, tx: G.tf.x, ty: G.tf.y, moved: false}; cv.classList.add("drag"); };
  if(!G.wiredW){ G.wiredW = true; window.addEventListener("mouseup", () => { G.drag = null; const c2 = document.getElementById("gxc"); if(c2) c2.classList.remove("drag"); }); }
  cv.onmousemove = e => {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    if(G.drag){
      const dx = e.clientX - G.drag.x, dy = e.clientY - G.drag.y;
      if(Math.abs(dx) + Math.abs(dy) > 3) G.drag.moved = true;
      G.tf.x = G.drag.tx + dx; G.tf.y = G.drag.ty + dy; G.moved = true; tip.hidden = true; gxRequest(); return;
    }
    const h = gxPick(mx, my);
    if(h !== G.hover){ G.hover = h; gxRequest(); }
    if(h < 0){ tip.hidden = true; G.cluster = null; return; }
    const d = G.nodes[h];
    let near = 0;
    for(let i = 0; i < G.n; i++){ if(i !== h && G.vis[i] && Math.abs(G.sx[i] - mx) < 12 && Math.abs(G.sy[i] - my) < 12) near++; }
    const ttl = pv(((d.content || "").split("\n").find(s => s.trim()) || d.id).trim().slice(0, 90));
    G.cluster = gxCluster(h, mx, my); gxNoteRoom(d);
    const cl = G.cluster ? `<br><span style="color:var(--brand)">${G.cluster.n.toLocaleString()} drawers in this room${G.cluster.span ? ", " + esc(G.cluster.span) : ""}: a pile written in one room. <b>L</b> lists them by date (click the map first; L does not work while the search box has focus).</span>` : `<br><span style="color:var(--fg-subtle)"><b>L</b> lists this room by date (not while typing in the search box; use the button)</span>`;
    tip.innerHTML = `<b>${esc(ttl)}</b><br><span style="color:var(--fg-subtle)">${esc(d.wing)} / ${esc(d.room)}${near ? " · " + near + " more dot" + (near === 1 ? "" : "s") + " here: zoom in" : ""}</span><br>filed ${esc((d.filed_at || "").slice(0, 10))}${d.added_by ? " by " + esc(d.added_by) : ""}${
      G.mod[h] ? " · modified " + esc(gxYmd(G.mod[h])) : ""}${G.nrev && G.nrev[h] ? " · text rewritten " + G.nrev[h] + "×, last " + esc(gxYmd(G.touch[h])) : ""}${G.sup && G.sup[h] ? ' · <span style="color:#22e0d0">a newer drawer replaces this one</span>' : ""}${
      G.secret && G.secret.has(d.id) ? ' · <span style="color:var(--error)">possible secret</span>' : ""}${cl}`;
    tip.hidden = false;
    tip.style.left = Math.min(r.width - 350, mx + 14) + "px"; tip.style.top = Math.min(r.height - 120, my + 14) + "px";
  };
  cv.onmouseleave = () => { G.hover = -1; G.cluster = null; tip.hidden = true; gxRequest(); };
  cv.onclick = e => {
    if(G.drag && G.drag.moved) return;
    if(G.privacy) return;
    const r = cv.getBoundingClientRect(), h = gxPick(e.clientX - r.left, e.clientY - r.top);
    if(h >= 0 && G.nodes[h] && G.nodes[h].content !== undefined) detail(G.nodes[h]);
    else if(h < 0 && G.target >= 0) gxClearTarget();
  };
  cv.addEventListener("wheel", e => {
    e.preventDefault();
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top, f = Math.exp(-e.deltaY * 0.0015);
    const k2 = Math.max(0.05, Math.min(80, G.tf.k * f)), s = k2 / G.tf.k;
    G.tf.x = mx - (mx - G.tf.x) * s; G.tf.y = my - (my - G.tf.y) * s; G.tf.k = k2; G.moved = true; gxRequest();
  }, {passive:false});
  if(!G.wiredR){ G.wiredR = true; window.addEventListener("resize", () => { if(document.getElementById("gxc")){ if(!G.moved && G.P) gxFit(false); gxRequest(); } }); }
}

async function loadHistory(d){
  const box = document.getElementById("revBox"); if(!box) return;
  box.innerHTML = `<div class="rv-h">History</div><div class="rv-n">Loading…</div>`;
  try{
    const key = (d.meta && d.meta.parent_drawer_id) || d.id;                // revisions are kept per parent drawer, not per chunk
    const r = await fetch("/api/revisions?id=" + encodeURIComponent(key)), j = await r.json();
    if(!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    if(document.getElementById("revBox") !== box) return;                 // the user moved on
    const revs = (j.revisions || []).filter(v => !v.same), nMoved = (j.count || 0) - (j.rewrites == null ? j.count : j.rewrites);
    if(!j.count){ box.innerHTML = `<div class="rv-h">History</div><div class="rv-n">Never edited: this is the version that was filed.</div>`; return; }
    if(!revs.length){ box.innerHTML = `<div class="rv-h">History</div><div class="rv-n">The words have never changed. It was moved or re-chunked ${j.count} time${j.count === 1 ? "" : "s"} (MemPalace writes a revision for that); the text is the version that was filed.</div>`; return; }
    const gi = (G.idxOf && G.idxOf.has(d.id)) ? G.idxOf.get(d.id) : -1;
    const cur = `<div class="rv cur"><b>current</b> · ${bytes(d.bytes)}${gi >= 0 && G.rew && G.rew[gi] ? " · rewritten " + gxYmd(G.rew[gi]) : ""}</div>`;
    const olds = revs.slice().reverse().map(v => `<div class="rv old">
        <div><b>superseded ${esc(isNaN(Date.parse(v.recorded_at)) ? (v.recorded_at || "") : gxYmd(Date.parse(v.recorded_at)) + " " + new Date(Date.parse(v.recorded_at)).toTimeString().slice(0, 5))}</b> · ${v.chars.toLocaleString()} chars${
          v.secrets ? ` · <span style="color:var(--error)">contains ${v.secrets} credential-shaped value${v.secrets === 1 ? "" : "s"} (masked)</span>` : ""}</div>
        <div class="rv-p">${esc(v.preview)}${v.chars > v.preview.length ? "…" : ""}</div>${
          (v.full || "").length > v.preview.length ? '<a class="rv-more" role="button" tabindex="0">show more</a>' : ""}</div>`).join("");
    box.innerHTML = `<div class="rv-h">History · text rewritten ${revs.length} time${revs.length === 1 ? "" : "s"}${key !== d.id ? " (whole drawer, all chunks)" : ""}${nMoved > 0 ? " · moved or re-chunked " + nMoved + "×" : ""}</div>` + cur + olds
      + `<div class="rv-n">Earlier versions are kept by MemPalace's revision log, and a copy sits in every backup. Credential-shaped values are masked here.</div>`;
    const oldsList = revs.slice().reverse();
    box.querySelectorAll(".rv.old").forEach((el, k) => {
      const more = el.querySelector(".rv-more"), p = el.querySelector(".rv-p"), v = oldsList[k];
      if(!more) return;
      const flip = () => {                              // textContent, not innerHTML: version text is data
        const open = more.dataset.open === "1";
        p.textContent = open ? v.preview + (v.chars > v.preview.length ? "…" : "") : v.full + (v.full.length >= 6000 ? "\n… (first 6,000 characters)" : "");
        more.dataset.open = open ? "0" : "1"; more.textContent = open ? "show more" : "show less";
      };
      more.onclick = flip; more.onkeydown = e => { if(e.key === "Enter" || e.key === " "){ e.preventDefault(); flip(); } };
    });
  }catch(e){ box.innerHTML = `<div class="rv-h">History</div><div class="rv-n">Could not load: ${esc(String(e && e.message ? e.message : e))}</div>`; }
}
function aboutHtml(){
  // rel="noopener noreferrer" on every outbound link: target="_blank" otherwise
  // hands the opened page a handle back to this one through window.opener.
  const repo = DATA.repo_url || "";
  return `<div class="about">
    <img class="ic" src="/icon.svg" alt="">
    <div>
      <div class="nm">MemPalace Browser <b>${esc(DATA.mpb_version || "—")}</b>
        <span class="bld" title="sha256 of the running app.py — compare with the repo">build ${esc(DATA.build_id || "—")}</span></div>
      <a class="by" href="https://henderlabs.com" target="_blank"
         rel="noopener noreferrer">henderlabs.com</a>
      <div class="sub" style="margin-top:5px">
        An unofficial companion to
        <a href="${MP_REPO}" target="_blank" rel="noopener noreferrer"
           title="${MP_REPO}">MemPalace ↗</a>, which is MIT-licensed and
        © its contributors. Not affiliated with that project.
        ${repo ? `<br><a href="${esc(repo)}" target="_blank" rel="noopener noreferrer"
                    title="${esc(repo)}">This browser's source ↗</a>` : ""}</div>
    </div>
  </div>`;
}

// The palace advertises six layers beyond the drawer. Most palaces have never
// written to most of them, and nothing anywhere says so — an empty knowledge
// graph answers a query with count:0, which reads as "no such facts" rather
// than "no such layer". This panel is the difference between those two, and it
// is the reason a coverage number appears next to every row instead of a tick.
// Host figures, labelled as the host. Hidden entirely unless asked for.
function resourcesHtml(){
  const r = DATA.resources || {};
  if(!r.enabled) return "";
  if(!r.available)
    return `<div class="st-h">Host</div>
            <div class="st-note" style="color:var(--fg-subtle)">${esc(r.reason||"unavailable")}</div>`;
  const pct = r.mem_total ? (r.mem_used / r.mem_total * 100) : 0;
  const cls = pct > 90 ? "bad" : pct > 75 ? "warn" : "";
  // Load is shown against core count, because 0.32 means nothing until you know
  // whether the box has one core or thirty-two.
  const lpc = Math.min(100, (r.load1 / (r.cpus||1)) * 100);
  const lcls = lpc > 100 ? "bad" : lpc > 70 ? "warn" : "";
  return `<div class="st-h">Host</div>
    <div class="st-row"><span>Load (1m)</span><span>${r.load1.toFixed(2)} / ${r.cpus} cpu</span></div>
    <div class="bar ${lcls}"><i style="width:${Math.max(0.6,lpc).toFixed(1)}%"></i></div>
    <div class="st-row" style="margin-top:5px"><span>Memory</span><span>${bytes(r.mem_used)} / ${bytes(r.mem_total)}</span></div>
    <div class="bar ${cls}"><i style="width:${Math.max(0.6,pct).toFixed(1)}%"></i></div>
    <div class="st-row"><span>Available</span><span>${bytes(r.mem_avail)}</span></div>
    ${r.swap_total ? `<div class="st-row"><span>Swap</span><span>${bytes(r.swap_used)} / ${bytes(r.swap_total)}</span></div>` : ""}
    <div class="st-note">this machine, not MemPalace — the embedder and the vector index live in RAM</div>`;
}

function layersHtml(){
  const L = DATA.layers || {};
  const total = DATA.count || 1;
  const rows = [
    ["Drawers",  L.drawers && L.drawers.n,  total, null],
    ["Closets",  L.closets && L.closets.n,  total, L.closets && L.closets.error],
    ["Halls",    L.halls && L.halls.n,      total, null],
    ["Entities", L.entities && L.entities.n, total, null],
    ["Hallways", L.hallways && L.hallways.n, null, L.hallways && L.hallways.error],
    ["Tunnels (set)",  L.tunnels_explicit && L.tunnels_explicit.n, null,
                 L.tunnels_explicit && L.tunnels_explicit.error],
  ];
  let html = rows.map(([k, n, denom, err]) => {
    if(err !== null && err !== undefined)
      return `<div class="k">${k}</div><div class="v none" style="grid-column:2/4">unavailable</div>`;
    if(n === null || n === undefined)
      return `<div class="k">${k}</div><div class="v none" style="grid-column:2/4">unknown</div>`;
    const pct = denom ? Math.round(n / denom * 100) : (n ? 100 : 0);
    const label = denom ? `${n} · ${pct}%` : `${n}`;
    return `<div class="k">${k}</div>
            <div class="track"><i class="${n?"":"zero"}" style="width:${n?Math.max(2,pct):100}%"></i></div>
            <div class="v${n?"":" none"}">${label}</div>`;
  }).join("");

  // The knowledge graph counts facts, not drawers, so it gets its own row
  // rather than a coverage bar that would compare two different things.
  const kg = L.kg || {};
  if(kg.error !== undefined)
    html += `<div class="k">Graph</div><div class="v none" style="grid-column:2/4">unavailable</div>`;
  else
    html += `<div class="k">Graph</div>
             <div class="v${kg.entities||kg.triples?"":" none"}" style="grid-column:2/4">
               ${kg.entities||0} entities · ${kg.triples||0} triples</div>`;

  // A second graph file next to the one we read means an agent talking to this
  // same palace may be getting a different answer than the number above.
  // Naming it is the only way anyone finds out.
  const rival = kg.rival
    ? `<div class="st-note" style="color:var(--warning);margin-top:6px">
         ⚠ A second knowledge_graph.sqlite3 exists at <b>${esc(kg.rival)}</b>.
         An MCP server started with <b>--palace</b> reads that one, not this one.</div>` : "";

  const passive = (L.tunnels_passive && L.tunnels_passive.n) || 0;
  return `<div class="lay">${html}</div>
    <div class="st-note" style="margin-top:7px">${passive} more implied by room names shared across wings
      (derived, not stored, so not in the Tunnels count above).</div>${rival}`;
}

function gxListHost(){
  const content = document.getElementById("content");
  if(!document.getElementById("listPane")) content.innerHTML = '<div id="listPane"></div><div id="gxPane"></div>';
  gxBuild(document.getElementById("gxPane"), true);
  return document.getElementById("listPane");
}
function renderList(){
  const hits = DATA.drawers.filter(matches);
  const secretIds = new Set(((DATA.secrets || {}).ids) || []);
  const c = gxListHost();
  const crumb = `<div class="crumb"><a data-nav="all">All wings</a>`
    + (sel.wing ? ` / <a data-nav="wing" data-w="${esc(sel.wing)}">${esc(sel.wing)}</a>` : "")
    + (sel.room ? ` / ${esc(sel.room)}` : "")
    + ` &nbsp;·&nbsp; ${hits.length} drawer${hits.length===1?"":"s"}${query?` matching “${esc(query)}”`:""}`
    + (query && (sel.wing || sel.room) ? ` <span style="color:var(--brand)">&nbsp;·&nbsp; search limited to ${esc(sel.wing || "")}${sel.room ? "/" + esc(sel.room) : ""}: <a data-nav="all">search all wings</a></span>` : "")
    + `<span class="xa"> &nbsp;·&nbsp; <a data-xa="1">expand first 50</a> · <a data-xa="0">collapse all</a></span></div>`;
  if(!hits.length){
    c.innerHTML = crumb + `<div class="empty">No drawers match.</div>`;
    wireNav(c); return;
  }
  const cardOf = d => {
    const open = G.open.has(d.id), i = DATA.drawers.indexOf(d);
    let prev = d.content.slice(0, 240);
    if(query){
      const k = d.content.toLowerCase().indexOf(query.toLowerCase());
      if(k > 90) prev = "…" + d.content.slice(k - 60, k + 180);
    }
    const mod = (d.meta || {}).last_modified, edited = mod && Date.parse(mod) > Date.parse(d.filed_at) + 120000;
    return `<div class="card${isNew(d) ? " isnew" : ""}${open ? " open" : ""}" data-i="${i}" tabindex="0" role="button" aria-expanded="${open}">
      <div class="top"><span class="chev">${open ? "▾" : "▸"}</span><span class="loc">${esc(d.wing)} / ${esc(d.room)}</span>
        ${isNew(d) ? '<span class="pill">new</span>' : ""}
        ${secretIds.has(d.id) ? '<span class="pill sec" title="Text in this drawer is shaped like a credential. See System status.">secret?</span>' : ""}
        <span class="when">${when(d.filed_at)}</span></div>
      ${open
        ? `<div class="meta">filed ${esc((d.filed_at || "").slice(0, 16).replace("T", " "))}${d.added_by ? " by " + esc(d.added_by) : ""}${
             edited ? " · edited " + esc(String(mod).slice(0, 16).replace("T", " ")) : ""} · ${bytes(d.bytes)}</div>
           <pre class="full">${hl(d.content, query)}</pre>
           <div class="acts"><button data-act="open">Open full view · history</button><button data-act="map">Show on map</button><button data-act="close">Collapse</button></div>`
        : `<div class="prev">${hl(pv(prev), query)}</div>`}</div>`;
  };
  c.innerHTML = crumb + hits.map(cardOf).join("");
  wireNav(c);
  const wireCard = el => {
    const toggle = () => {                           // swap just this card, so the list does not jump
      const d = DATA.drawers[+el.dataset.i];
      if(G.open.has(d.id)){ G.open.delete(d.id); if(G.targetId === d.id) gxClearTarget(); }
      else { G.open.add(d.id); gxSetTarget(DATA.drawers.indexOf(d), false); }
      const t = document.createElement("div"); t.innerHTML = cardOf(d);
      const nu = t.firstElementChild; el.replaceWith(nu); wireCard(nu); nu.focus({preventScroll:true});
    };
    el.onclick = e => {
      if(e.target.closest(".full")) return;          // selecting text must not collapse the card
      const b = e.target.closest("[data-act]");
      if(b){
        e.stopPropagation(); const a = b.dataset.act;
        if(a === "open") detail(DATA.drawers[+el.dataset.i]); else if(a === "map") gxLocate(+el.dataset.i); else toggle();
        return;
      }
      toggle();
    };
    el.onkeydown = e => { if(e.target === el && (e.key === "Enter" || e.key === " ")){ e.preventDefault(); toggle(); } };
    el.onmouseenter = () => gxFocus(+el.dataset.i);
    el.onmouseleave = () => gxFocus(-1);
  };
  c.querySelectorAll(".card").forEach(wireCard);
  c.querySelectorAll("[data-xa]").forEach(a => a.onclick = () => {
    if(a.dataset.xa === "1") hits.slice(0, 50).forEach(d => G.open.add(d.id)); else G.open.clear();
    renderList();
  });
  c.scrollTop = 0;
}

function detail(d){
  const skip = new Set(["wing","room"]);
  const rows = Object.entries(d.meta).filter(([k]) => !skip.has(k))
    .map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  const c = document.getElementById("listPane") || document.getElementById("content");   // keep the map beside it

  // If this drawer is a slice of something longer, say so before showing it.
  // Otherwise a chunk that opens mid-sentence reads as damaged data rather
  // than as page 9 of 27.
  const doc = docOfDrawer[d.id];
  let partof = "";
  if(doc){
    const pos = doc.ids.indexOf(d.id) + 1;
    partof = `<div class="partof">Chunk <b>${pos}</b> of ${doc.n} from
      <a data-doc="${esc(doc.key)}">${esc(doc.key)}</a> — open the whole document to read it in order.</div>`;
  }
  c.innerHTML = `
    <div class="crumb"><a data-back>← back</a> &nbsp;·&nbsp;
      <a data-nav="wing" data-w="${esc(d.wing)}">${esc(d.wing)}</a> /
      <a data-nav="room" data-w="${esc(d.wing)}" data-r="${esc(d.room)}">${esc(d.room)}</a></div>
    <div class="detail"><h2>${esc(d.id)}</h2>
      ${partof}
      <dl class="metagrid">${rows}<dt>size</dt><dd>${bytes(d.bytes)}</dd></dl>
      <pre class="doc">${hl(d.content, query)}</pre><div id="revBox" class="revbox"></div><div id="chainBox" class="revbox"></div></div>`;
  wireNav(c);
  gxFocus(DATA.drawers.indexOf(d)); loadHistory(d); gxHistoryChain(d);
  c.querySelector("[data-back]").onclick = () => { gxFocus(-1); render(); };
  const dl = c.querySelector("[data-doc]");
  if(dl) dl.onclick = () => docDetail(doc);
  c.scrollTop = 0;
}

// ---- documents -------------------------------------------------------------
// A document is every chunk mined from one source_file, back in order. It is
// deliberately not part of the tree: chunks are filed by topic, so one file's
// chunks legitimately land in different rooms.
function docMatches(doc){
  if(!query) return true;
  const q = query.toLowerCase();
  if(doc.key.toLowerCase().includes(q)) return true;
  return doc.ids.some(id => byId[id] && byId[id].content.toLowerCase().includes(q));
}

function renderDocs(){
  const hits = (DATA.documents || []).filter(docMatches);
  const c = document.getElementById("content");
  const loose = DATA.count - (DATA.documents||[]).reduce((a,d)=>a+d.n,0);
  const crumb = `<div class="crumb">Documents &nbsp;·&nbsp; ${hits.length} of
    ${(DATA.documents||[]).length}${query?` matching “${esc(query)}”`:""}
    &nbsp;·&nbsp; <span style="color:var(--fg-subtle)">reassembled from
    source_file — the remaining ${loose} drawer${loose===1?"":"s"}
    ${loose===1?"stands":"stand"} alone</span></div>`;
  if(!hits.length){
    c.innerHTML = crumb + `<div class="empty">No documents match.</div>`;
    return;
  }
  c.innerHTML = crumb + hits.map((doc, i) => {
    const first = byId[doc.ids[0]];
    const prev = first ? first.content.slice(0, 200) : "";
    const where = doc.rooms.length > 1
      ? `${doc.wings.join(", ")} · ${doc.rooms.length} rooms`
      : `${doc.wings.join(", ")} / ${doc.rooms[0]}`;
    return `<div class="card" data-d="${i}">
      <div class="top"><span class="loc">${esc(doc.key)}</span>
        <span class="nchunk">${doc.n} chunks</span>
        ${doc.issues.length ? '<span class="pill" style="background:var(--warning)">order</span>' : ""}
        <span class="when">${when(doc.last)}</span></div>
      <div class="docmeta">${esc(where)} · ${bytes(doc.bytes)}</div>
      <div class="prev">${hl(pv(prev), query)}</div></div>`;
  }).join("");
  c.querySelectorAll(".card").forEach(el =>
    el.onclick = () => docDetail(hits[+el.dataset.d]));
  c.scrollTop = 0;
}

function docDetail(doc){
  const c = document.getElementById("content");
  // Chunk seams stay visible. This is verbatim slices shown in order, not a
  // file we recovered — and each seam is a filing decision the miner made,
  // which is worth being able to see rather than smoothing away.
  const body = doc.ids.map((id, i) => {
    const d = byId[id];
    if(!d) return "";
    const ci = d.chunk_index === null || d.chunk_index === undefined ? "—" : d.chunk_index;
    return `<div class="seam">chunk ${ci} · <span class="rm">${esc(d.wing)}/${esc(d.room)}</span></div>
            <pre>${hl(d.content, query)}</pre>`;
  }).join("");

  const warn = doc.issues.length
    ? `<div class="note"><b>Chunk order is not reliable for this document.</b>
       ${doc.issues.map(esc).join("; ")}. Shown in the best order available
       (chunk_index, then filing time) — but MemPalace's own numbering is the
       problem here, so treat the sequence as a hint.</div>` : "";
  const spans = doc.rooms.length > 1
    ? `<div class="note" style="border-left-color:var(--brand);background:rgb(13 107 255 / .07)">
       This file's chunks are filed across <b>${doc.rooms.length} rooms</b>
       (${doc.rooms.map(esc).join(", ")}) — the miner working as designed, since
       rooms are topics and one file covers several. Chunk numbers restart at 0
       in each room, so this is shown as one run per room, not one sequence.
       It is also why documents are a separate view rather than a branch of the
       tree: this file has no single place to live.</div>` : "";

  c.innerHTML = `
    <div class="crumb"><a data-back>← back</a> &nbsp;·&nbsp; Document</div>
    <div class="detail"><h2>${esc(doc.key)}</h2>
      <dl class="metagrid">
        <dt>chunks</dt><dd>${doc.n}</dd>
        <dt>wings</dt><dd>${doc.wings.map(esc).join(", ")}</dd>
        <dt>rooms</dt><dd>${doc.rooms.map(esc).join(", ")}</dd>
        <dt>filed</dt><dd>${when(doc.first)}${doc.first!==doc.last?" → "+when(doc.last):""}</dd>
        <dt>size</dt><dd>${bytes(doc.bytes)}</dd></dl>
      ${warn}${spans}
      <div class="chunks">${body}</div></div>`;
  c.querySelector("[data-back]").onclick = () => { render(); };
  c.scrollTop = 0;
}

// Everything that could legitimately stop this from being "healthy", each with
// a fix a person -- or their agent -- can actually act on.
//
// This program does not apply fixes and must not. It is read-only by
// construction, and it answers on 0.0.0.0 with no authentication: a "Fix it"
// button here would be unauthenticated remote command execution on the machine
// holding your memory. Diagnosing is our job; acting is the operator's, or the
// agent they prompt with it. MemPalace is a tool for agents -- handing one an
// accurate diagnosis IS the actuator.
//
// Every advisory carries verify and undo, not just a command. A warning that
// says "run this" without telling you how to check it worked or how to back it
// out is asking a stranger to trust us blindly.
//
// Where two fixes are legitimate, both are shown with the tradeoff stated. The
// knowledge-graph split has exactly this shape, and picking for the operator
// would have been wrong -- symlink and move are both correct, for different
// deployments.
function healthIssues(){
  const out = [];
  const L = DATA.layers || {}, kg = L.kg || {}, s = DATA.storage || {}, v = DATA.version || {};

  for(const w of (DATA.palace_notes || [])){
    if(/embedder identity/i.test(w)){
      out.push({level:"warn",
        title:"Embedder identity is not recorded on this palace",
        why:"MemPalace is assuming the current model. Nothing is wrong today, but if the "
           +"default embedder ever changes, new drawers get embedded into a different vector "
           +"space than the ones already filed. Semantic search does not error on that — it "
           +"quietly returns worse answers. Recording the identity now pins it.",
        fixes:[{label:"Record the identity", cmd:"mempalace palace set-embedder --model minilm"}],
        verify:"Restart the browser. This warning should be gone and System status should read "
              +"0 warnings. (The warning is raised once per process, so it cannot clear itself "
              +"without a restart.)",
        undo:"Re-run with --force and a different --model to change it. It writes palace "
            +"metadata only; drawers and embeddings are untouched."});
    } else {
      out.push({level:"warn", title:"MemPalace reported a warning about this palace", why:w});
    }
  }

  if(kg.rival){
    out.push({level:"warn",
      title:"Two knowledge graphs — your agents may be reading the empty one",
      why:`This browser reads ${kg.path} (${kg.entities||0} entities, ${kg.triples||0} triples). `
         +`An MCP server started with --palace reads ${kg.rival} instead. mcp_server._resolve_kg_path() `
         +`returns <palace_path>/knowledge_graph.sqlite3 when --palace was passed and the library `
         +`default otherwise — different files even when --palace names the default path. `
         +`KnowledgeGraph() creates a missing file rather than failing, so the losing side answers `
         +`kg_query with a confident count:0 about facts that exist. Note --palace is a REQUIRED `
         +`argument to 'daemon serve', so it cannot simply be dropped.`,
      fixes:[
        {label:"A. Symlink (nothing moves; recommended)",
         cmd:`# stop the MCP daemon AND this browser first — swapping a file under a\n`
            +`# process that holds it open leaves it reading the old inode.\n`
            +`mv ${kg.rival} ${kg.rival}.empty.bak\n`
            +`rm -f ${kg.rival}-wal ${kg.rival}-shm\n`
            +`ln -s ${kg.path} ${kg.rival}`,
         note:"Safe: SQLite resolves the link and keeps -wal/-shm beside the real file, so both "
             +"names are one database with one WAL."},
        {label:"B. Move the real graph to where the daemon looks",
         cmd:`# stop the MCP daemon AND this browser first.\n`
            +`mv ${kg.rival} ${kg.rival}.empty.bak\n`
            +`mv ${kg.path} ${kg.rival}\n`
            +`ln -s ${kg.rival} ${kg.path}`,
         note:"Prefer this if the daemon is the only writer and you want the real file living in "
             +"the palace directory. Same end state, opposite direction."}],
      verify:`readlink -f ${kg.rival}\n# must resolve to ${kg.path}\n`
            +`# then restart both and confirm kg_stats reports the real counts.`,
      undo:`rm ${kg.rival} && mv ${kg.rival}.empty.bak ${kg.rival}`});
  }

  if(s.available && s.disk_total){
    const pct = s.disk_used / s.disk_total * 100;
    if(pct > 75) out.push({level: pct > 90 ? "bad" : "warn",
      title:`Disk ${pct.toFixed(0)}% full on ${s.mount}`,
      why:s.dedicated ? "This volume is dedicated to MemPalace, so it is your palace that runs out."
                      : "This volume is shared with the OS — your palace is competing with everything "
                       +"else on the machine for what is left.",
      fixes:[{label:"Free space or grow the volume", cmd:`du -sh ${s.data_path||s.mount}/* | sort -h | tail`}],
      verify:"Reload this page; the storage bar should drop below 75%.",
      undo:"n/a — nothing here is destructive."});
  }

  if(v.status === "unknown")
    out.push({level:"warn", title:"Update check failed — could not reach PyPI",
      why:"The installed version is known; whether it is current is not. This is a network fact, "
         +"not a palace fact. Said out loud rather than shown as 'up to date', which would be a guess.",
      fixes:[{label:"Silence the check (makes this program fully offline)", cmd:"MPB_CHECK_UPDATES=0 ./run.sh"}],
      verify:"The version chip should read 'update check off' rather than 'update check failed'.",
      undo:"Unset MPB_CHECK_UPDATES."});

  if(v.status === "update-available")
    out.push({level:"warn", title:`MemPalace ${v.latest} is available (running ${v.installed})`,
      why:"Informational. Upgrading MemPalace can change the palace format — 'mempalace migrate' "
         +"exists precisely because that schema moves. Read their changelog first.",
      fixes:[{label:"Upgrade (backup first, matched to how MemPalace is installed here)", cmd:mpUpgradeSteps(),
              note:"The upgrade from 3.8.0 to 3.12.0 changed wing naming and the search index, which is why the backup comes first."}],
      verify:"Restart this browser; the version chip should read 'up to date'.",
      undo:"Reinstall the previous version with the same tool."});

  const op = DATA.ops || {}, ig = op.integrity || {}, bk = op.backups || {};
  const sc = DATA.secrets || {}, rs = DATA.revsecrets || {};
  if(rs.versions){
    out.push({level:"warn",
      title:`Credential-shaped text survives in ${rs.versions} old version${rs.versions === 1 ? "" : "s"} (revision log)`,
      why:"Editing or redacting a drawer does not remove its earlier text. MemPalace keeps every earlier version in its revision log, "
         +"the SQLite file holds old pages, and every backup is a copy. The drawers listed are clean now; their old versions are not. "
         +"Rotate or revoke the credential; scrubbing the log is a separate job. Values are masked here.",
      fixes:[{label:"Which drawers (ids)", cmd:(rs.drawers || []).slice(0, 12).join("\n"),
              note:"Open one: its History section lists each older version with the values masked."}],
      verify:"Reload this page; the count falls when the log has been scrubbed.", undo:"n/a — nothing here changes anything."});
  }
  const mu = DATA.mpb_update || {};
  if(mu.status === "update-available"){
    out.push({level:"info", title:`MemPalace Browser ${mu.latest} is available (running ${mu.current})`,
      why:"A newer release of this browser exists. Informational: nothing is wrong. This browser never updates itself; "
         +"updating is a script you run, which checks the download against the release's published checksum, keeps a backup, "
         +"restarts the service, and puts the old file back if the new one does not come up healthy.",
      fixes:[{label:"Update (run on the machine that hosts this browser)", cmd:mu.cmd || "bash update.sh",
              note:"Add --check to look without changing anything."},
             {label:"What changed", cmd:mu.url || "(the release has no page link)"}],
      verify:"Reload this page: the Updates row should read 'up to date'.",
      undo:"bash update.sh --rollback   # in the folder that holds app.py"});
  }
  if(sc.enabled && (sc.tokenids || []).length){
    const t = sc.tokenids;
    out.push({level:"info",
      title:`Proxmox token IDs in ${t.length} drawer${t.length === 1 ? "" : "s"} (names, not secrets)`,
      why:"A token ID such as user@realm!name is the account half of an API credential. On its own it cannot log in, "
         +"but it tells anyone who reads the palace which account to target, and a note often keeps the ID after the "
         +"secret is gone. If the token still exists, check that it is needed and as narrow as it can be. "
         +"IDs are shortened here and are not counted as secrets.",
      fixes:[{label:"Where (shortened)", cmd:t.slice(0, 12).map(f => `${f.masked} · ${f.wing}/${f.room} · ${f.id}`).join("\n")
                + (t.length > 12 ? `\n… and ${t.length - 12} more` : ""),
              note:"Only the ID is shown; the drawer text is unchanged."},
             {label:"Is the token still in use? (Proxmox host)", cmd:"pveum user token list <user@realm>",
              note:"Revoke an unused token with: pveum user token remove <user@realm> <tokenname>. Your call, not MPB's."},
             {label:"Not a concern? Stop listing a drawer", cmd:"Environment=MPB_SECRET_ALLOW=<drawer id>[,<drawer id>…]",
              note:"Hides that drawer from the secret and token-ID scans only."}],
      verify:"Reload this page: the count above should change.",
      undo:"Nothing here changes anything."});
  }
  if(sc.enabled && sc.total){
    const fnd = sc.findings || [], shown = fnd.slice(0, 12);
    const where = shown.map(f => `${f.kind} · ${f.wing}/${f.room} · ${f.id}\n    ${f.ctx}`).join("\n")
      + (sc.total > shown.length ? `\n… and ${sc.total - shown.length} more` : "");
    const kinds = Object.entries(sc.by_kind || {}).map(([k, n]) => `${k} ×${n}`).join(", ");
    out.push({level:"warn",
      title:`Possible secrets in ${sc.drawers} drawer${sc.drawers === 1 ? "" : "s"} (${sc.total} match${sc.total === 1 ? "" : "es"})`,
      why:`Text shaped like a credential was found (${kinds}). This is a pattern scan: it can flag things that `
         +`are not secrets, and it cannot tell whether a real one is still live. IMPORTANT: editing or deleting `
         +`a drawer does not remove the old text. MemPalace keeps earlier versions in its revision log, the SQLite `
         +`file holds old pages, and every backup is a copy. Rotate or revoke the credential first; scrubbing the `
         +`drawer is cleanup, not remediation. Matches are masked here — MPB never prints a full match in a warning.`,
      fixes:[
        {label:"Where (masked)", cmd: where,
         note:"Ids are the drawer ids shown in the list, where a 'secret?' badge marks them."},
        {label:"After rotating: replace the value in the drawer (ask your agent)",
         cmd:`mempalace_update_drawer drawer_id=<id above> content="<same text, value replaced with [REDACTED]>"`,
         note:"Changes only that drawer's text. The old text still persists in the revision log and in backups, "
             +"which is why rotation comes first."},
        {label:"Not a secret? Stop flagging a drawer",
         cmd:`# in the browser's service unit, then restart the browser\nEnvironment=MPB_SECRET_ALLOW=<drawer id>[,<drawer id>…]`,
         note:"Hides that drawer from this scan only. Nothing else changes."}],
      verify:"Reload this page: the drawer should lose its 'secret?' badge and this warning should list fewer matches.",
      undo:"Nothing here changes anything. Remove MPB_SECRET_ALLOW to flag a drawer again."});
  }
  if(ig.checked && (ig.errors || []).length){
    out.push({level:"bad",
      title:"SQLite integrity check failed",
      why:"MemPalace's own quick_check found: " + ig.errors.slice(0,3).join("; ")
         +". This is the layer under every drawer, so searches and writes may misbehave or fail.",
      fixes:[{label:"Compare the database with the vector index (read-only)", cmd:"mempalace repair-status"}],
      unverified:"No repair command is offered here. Take a fresh copy of the palace first, and "
                +"prefer restoring the newest good backup over repairing in place.",
      verify:"Reload this page; Trust should read 'Integrity ok'.",
      undo:"n/a — nothing here changes anything."});
  }
  if(bk.configured){
    const base = bk.dir || "~", pp = DATA.palace_path || "<palace>", dp = s.data_path || "<data>";
    const mk = {label:"Take a backup now (safe while MemPalace is running)",
      cmd:`D=${base}/mempalace-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$D/palace"\n`
         +`sqlite3 ${pp}/chroma.sqlite3 ".backup '$D/palace/chroma.sqlite3'"\n`
         +`sqlite3 ${dp}/knowledge_graph.sqlite3 ".backup '$D/knowledge_graph.sqlite3'"\n`
         +`rsync -a --exclude='chroma.sqlite3*' --exclude='knowledge_graph.sqlite3*' ${dp}/ "$D/"`,
      note:"Reads the live palace and writes only a new folder. sqlite3 .backup is consistent while "
          +"writers are active; a plain cp is not."};
    if(!bk.count){
      out.push({level:"warn", title:"No backup found at the configured location",
        why:"MPB_BACKUP_GLOBS matches nothing, so there is no known way back if the palace is damaged.",
        fixes:[mk], verify:"Reload this page; Trust should show a recent 'Last backup'.",
        undo:"Delete the new backup folder."});
    } else {
      const age = (op.now || 0) - bk.newest.mtime;
      if(age > 7*86400) out.push({level: age > 30*86400 ? "bad" : "warn",
        title:`Last backup is ${dur(age)} old`,
        why:"Anything filed since then exists only in the live palace. Backups are what make an "
           +"upgrade or a cleanup reversible.",
        fixes:[mk], verify:"Reload this page; Trust should show 'Last backup' as a few minutes ago.",
        undo:"Delete the new backup folder."});
    }
  }

  for(const doc of (DATA.documents || [])){
    if(!doc.issues.length) continue;
    out.push({level:"warn",
      title:`Document filed more than once: ${doc.key}`,
      why:`${doc.issues.join("; ")}. The same source was mined more than once without purging the `
         +`earlier drawers, so the palace holds two copies of this content and a search will return `
         +`both. chunk_index is scoped per (source_file, room), so this is counted per room — a file `
         +`spanning several rooms is not itself a fault.`,
      // Deliberately no command. A re-mine's purge semantics for DRAWERS (as opposed to closets,
      // which CLOSETS.md documents as purged per source_file) are not established, and
      // delete_by_source may be the cleaner path. An unverified fix is worse than none: this is a
      // diagnosis, and it says so.
      fixes:[],
      unverified:"No verified fix yet. `mempalace_delete_by_source` followed by a re-mine is the "
                +"likely path, but whether a re-mine purges prior drawers has not been confirmed — "
                +"so no command is offered here rather than one that might delete the wrong thing.",
      verify:"After any fix, this warning should disappear on reload.",
      undo:"Back up the palace before deleting drawers."});
  }
  return out;
}

function renderChips(){
  document.getElementById("cntChip").innerHTML = `<b>${DATA.count}</b> drawers`;
  const nu = DATA.drawers.filter(isNew).length;
  const chip = document.getElementById("newChip");
  chip.style.display = nu ? "" : "none";
  chip.textContent = nu + " new";

  // The zero case is a count, not a badge. "Healthy" alone reads as decoration
  // — it looks the same whether we checked and found nothing or never checked
  // at all. "Healthy · 0 warnings" is visibly the same counter that says
  // "9 warnings" when there are nine, so a green chip is evidence of watching
  // rather than an assertion of wellness.
  const issues = healthIssues();
  const hc = document.getElementById("healthChip");
  const bad = issues.some(i => i.level === "bad");
  const n = issues.filter(i => i.level !== "info").length;            // notes (level info) are listed but are not warnings
  const nInfo = issues.length - n;
  const plural = n === 1 ? "warning" : "warnings";
  if(bad){
    hc.className = "chip act bad"; hc.textContent = `Needs attention · ${n} ${plural}`;
  } else if(n){
    hc.className = "chip act warn"; hc.textContent = `${n} ${plural}`;
  } else {
    hc.className = "chip act ok"; hc.textContent = "Healthy · 0 warnings";
  }
  hc.title = (n || nInfo) ? issues.map(i => (i.level === "info" ? "ℹ " : "• ") + i.title).join("\n")
              : "Checked: embedder identity, knowledge-graph split, disk, "
                + "update check, document chunk ordering, SQLite integrity, backup age, secret-looking text. Nothing to report.";

  // Escape these too. They look trustworthy — one is our own package version,
  // the other comes from PyPI over HTTPS — but "the input is probably fine" is
  // not a security control, and esc() is free.
  // This chip is MemPalace's version, not the browser's. Unlabelled next to a
  // wordmark reading "MemPalace Browser" it reads as ours, which is the one
  // number it is not — the browser's own version lives in System status.
  const v = DATA.version, vc = document.getElementById("verChip");
  const inst = esc(v.installed), late = esc(v.latest);
  const who = `<span class="who">MemPalace</span> <b>${inst}</b>`;
  if(v.status === "current"){
    vc.className = "chip act current"; vc.innerHTML = `${who} · up to date`;
  } else if(v.status === "update-available"){
    vc.className = "chip act update"; vc.innerHTML = `${who} → ${late} available`;
  } else if(v.status === "ahead"){
    vc.className = "chip act"; vc.innerHTML = `${who} · ahead of PyPI (${late})`;
  } else if(v.status === "checking"){
    vc.className = "chip act"; vc.innerHTML = `${who} · checking…`;
  } else if(v.status === "disabled"){
    // We did not look. Say that, rather than implying anything about it.
    vc.className = "chip"; vc.innerHTML = `${who} · update check off`;
    vc.title = "MPB_CHECK_UPDATES=0 — no outbound requests are made";
  } else {
    // PyPI unreachable. Say so — never imply "current".
    vc.className = "chip act unknown"; vc.innerHTML = `${who} · update check failed`;
    vc.title = v.error || "could not reach PyPI";
  }
}

function render(){
  document.querySelectorAll(".view").forEach(b =>
    b.classList.toggle("sel", b.dataset.view === view));
  renderChips(); renderTree(); renderStats();
  const content = document.getElementById("content"), layout = view === "galaxy" ? "gx" : view === "drawers" ? "split" : "docs";
  if(content.dataset.layout !== layout){ content.innerHTML = ""; content.dataset.layout = layout; }   // a new layout starts clean
  content.classList.toggle("gx", layout === "gx"); content.classList.toggle("split", layout === "split");
  if(view === "documents") renderDocs(); else if(view === "galaxy") renderGalaxy(); else renderList();
}

// ---- overlays --------------------------------------------------------------
function sheet(html){
  document.getElementById("sheet").innerHTML =
    `<button class="close" data-x>Close</button>` + html;
  const o = document.getElementById("overlay");
  o.hidden = false;
  document.querySelector("[data-x]").onclick = closeSheet;
}
function closeSheet(){ document.getElementById("overlay").hidden = true; }
document.addEventListener("keydown", e => { if(e.key === "Escape"){ closeSheet(); if(typeof gxClearTarget === "function") gxClearTarget(); } });

function systemSheet(){
  const issues = healthIssues();
  const v = DATA.version || {}, s = DATA.storage || {};
  const nWarn = issues.filter(i => i.level !== "info").length, nNote = issues.length - nWarn;
  const state = issues.some(i=>i.level==="bad") ? "Needs attention"
              : nWarn ? "Warnings present" : "Healthy";
  const notes = issues.length
    ? issues.map((i, n) => advisoryHtml(i, n)).join("")
    : `<div class="sub" style="margin:0">Nothing to report.</div>`;
  sheet(`
    <h2>System status — ${esc(state)}</h2>
    <p class="sub">What this deployment is reading, and anything wrong with it.
      This browser only reads — it never applies a fix. Every advisory below is
      something you (or an agent you hand it to) can run.</p>
    <h3>Warnings (${nWarn})${nNote ? ` · notes (${nNote})` : ""}</h3>${notes}
    <p class="sub" style="margin:8px 0 0;font-size:11px">Checked every read:
      embedder identity · knowledge-graph split · disk headroom · update check ·
      document chunk ordering.</p>
    <h3>Palace</h3>
    <dl class="kv">
      <dt>Path</dt><dd>${esc(DATA.palace_path)}</dd>
      <dt>Backend</dt><dd>${esc(s.backend || "—")}</dd>
      <dt>Drawers</dt><dd>${DATA.count}</dd>
      <dt>Documents</dt><dd>${(DATA.documents||[]).length} reassembled from source_file</dd>
      <dt>Read at</dt><dd>${esc(DATA.read_at || "—")} (${ago(DATA.read_at)})</dd>
      <dt>MemPalace</dt><dd>${esc(v.installed || "—")}${
        v.status==="current" ? " (current)" :
        v.status==="update-available" ? " → " + esc(v.latest) + " available" :
        v.status==="disabled" ? " (update check off)" : " (update check failed)"}
        · <a href="${MP_REPO}" target="_blank" rel="noopener noreferrer"
             style="color:var(--brand)">project ↗</a></dd>
      <dt>This browser</dt><dd>MemPalace Browser ${esc(DATA.mpb_version || "—")} · build
        ${esc(DATA.build_id || "—")} — unofficial, not affiliated with the MemPalace project${DATA.repo_url
          ? ` · <a href="${esc(DATA.repo_url)}" target="_blank" rel="noopener noreferrer"
                 style="color:var(--brand)">source ↗</a>` : ""}</dd>
      <dt>Browser mode</dt><dd>read-only — every collection is opened create=False</dd>
    </dl>
    <h3>Storage</h3>
    <dl class="kv">${ s.available
      ? `<dt>Palace</dt><dd>${bytes(s.palace_bytes)}</dd>
         <dt>All data</dt><dd>${bytes(s.data_bytes)}</dd>
         <dt>Volume</dt><dd>${esc(s.mount)} — ${bytes(s.disk_free)} free${
            s.dedicated ? " (dedicated)" : " (shared with the OS)"}</dd>`
      : `<dt>Storage</dt><dd>${esc(s.reason||"n/a")}</dd>` }</dl>`);
  wireAdvisories(issues);
}

function advisoryHtml(i, n){
  const fixes = (i.fixes || []).map(f => `
    <div class="fx">
      <div class="fx-h">${esc(f.label)}</div>
      <pre class="fx-c">${esc(f.cmd)}</pre>
      ${f.note ? `<div class="fx-n">${esc(f.note)}</div>` : ""}
    </div>`).join("");
  const unver = i.unverified ? `<div class="fx-u">${esc(i.unverified)}</div>` : "";
  return `<div class="adv ${i.level==="bad"?"bad":i.level==="info"?"info":""}">
    <div class="adv-t">${esc(i.title)}</div>
    <div class="adv-w">${esc(i.why)}</div>
    ${fixes}${unver}
    ${i.verify ? `<div class="fx-m"><b>Verify</b><pre class="fx-c">${esc(i.verify)}</pre></div>` : ""}
    ${i.undo ? `<div class="fx-m"><b>Undo</b><div class="fx-n">${esc(i.undo)}</div></div>` : ""}
    <button class="fx-b" data-adv="${n}">Copy for your agent</button>
  </div>`;
}

// Hands an agent the whole advisory -- diagnosis, commands, verification,
// rollback -- as one self-contained prompt. This is the actuator: the browser
// stays read-only and the agent does the work.
//
// Values that came from the palace (a source_file, say) are quoted as data and
// never interpolated into command position. A source_file is written by
// whatever mined the drawer, which makes it untrusted input; a fix prompt that
// splices it into a shell line is a prompt-injection vector aimed at the very
// agent being asked to run it.
function advisoryPrompt(i){
  const L = [];
  L.push("A read-only MemPalace Browser health check on my palace reports this. Please fix it.");
  L.push("");
  L.push("PALACE: " + (DATA.palace_path || "unknown"));
  L.push("REPORTED BY: MemPalace Browser " + (DATA.mpb_version||"") + " build " + (DATA.build_id||""));
  L.push("");
  L.push("WARNING: " + i.title);
  L.push("");
  L.push("WHY IT MATTERS:");
  L.push(i.why);
  if((i.fixes||[]).length){
    L.push("");
    L.push((i.fixes.length > 1)
      ? "PROPOSED FIXES (more than one is valid — ask me which I want before running anything):"
      : "PROPOSED FIX:");
    i.fixes.forEach(f => {
      L.push("");
      L.push("--- " + f.label);
      L.push(f.cmd);
      if(f.note) L.push("(" + f.note + ")");
    });
  }
  if(i.unverified){ L.push(""); L.push("NOTE: " + i.unverified); }
  if(i.verify){ L.push(""); L.push("VERIFY IT WORKED:"); L.push(i.verify); }
  if(i.undo){ L.push(""); L.push("ROLLBACK:"); L.push(i.undo); }
  L.push("");
  L.push("Please confirm the diagnosis against the machine before changing anything, "
       + "and tell me what you are going to run before you run it.");
  return L.join("\n");
}

function wireAdvisories(issues){
  document.querySelectorAll("[data-adv]").forEach(b => {
    b.onclick = async () => {
      const txt = advisoryPrompt(issues[+b.dataset.adv]);
      try { if(!(await copyText(txt))) throw new Error("no clipboard"); b.textContent = "Copied ✓"; }
      catch(e){
        // Clipboard needs a secure context; over plain http on a LAN name it is
        // unavailable. Say so and show the text rather than failing silently.
        b.textContent = "Select and copy ↓";
        const pre = document.createElement("pre");
        pre.className = "fx-c"; pre.style.marginTop = "6px"; pre.textContent = txt;
        b.after(pre);
      }
      setTimeout(() => { if(b.textContent === "Copied ✓") b.textContent = "Copy for your agent"; }, 1600);
    };
  });
}

// Shown once, unprompted, on a first visit. The vocabulary is the single
// biggest barrier for anyone who has not read MemPalace's docs — and the
// metaphor actively misleads: "palace" and "wing" make people go looking for
// hallways and closets as places you can walk into. They are not. Saying so up
// front costs four lines.
function vocabSheet(){
  sheet(`
    <h2>New to MemPalace?</h2>
    <p class="sub">Four words, and one thing the metaphor gets wrong.</p>
    <div class="vocab">
      <p><b>Wing</b> — a person or a project. The top level.</p>
      <p><b>Room</b> — a topic inside a wing.</p>
      <p><b>Drawer</b> — the memory itself: a slice of text, stored word for word.
         MemPalace never summarises or rewrites; a drawer is what was actually said.</p>
      <p><b>Document</b> — a browser feature, not a MemPalace one. Long files get
         split into many drawers, so a drawer often starts mid-sentence. The
         Documents view puts them back in order.</p>
      <p style="color:var(--fg-subtle);border-top:1px solid var(--line);padding-top:10px">
         The building is a naming convention, not a structure — there is no floor
         above a wing and nothing inside a drawer. Wings and rooms are labels the
         search filters on. Closets, halls and tunnels exist, but they point at
         drawers rather than contain them, and in most palaces they are empty.
         The <b>Layers</b> panel shows exactly how full yours are.</p>
      <p style="color:var(--fg-subtle)">This browser only reads. Nothing you click
         changes your palace.</p>
    </div>`);
}

async function load(force){
  const btn = document.getElementById("btnRefresh");
  btn.classList.add("spin"); btn.disabled = true;
  try {
    const r = await fetch("/api/data" + (force ? "?refresh=1" : ""));
    if(!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    DATA = d; readAt = d.read_at;

    // Index once per load, not once per render.
    byId = {}; docOfDrawer = {};
    for(const dr of d.drawers) byId[dr.id] = dr;
    for(const doc of (d.documents || [])) for(const id of doc.ids) docOfDrawer[id] = doc;
    document.getElementById("vnDrawers").textContent = d.count;
    document.getElementById("vnDocs").textContent = (d.documents || []).length;

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
document.querySelectorAll(".view").forEach(b => b.onclick = () => {
  view = b.dataset.view; render();
});
document.getElementById("healthChip").onclick = systemSheet;
document.getElementById("helpChip").onclick = vocabSheet;
document.getElementById("overlay").onclick = e => {
  if(e.target.id === "overlay") closeSheet();   // click the backdrop, not the sheet
};
document.getElementById("verChip").onclick = async () => {
  const vc = document.getElementById("verChip");
  vc.textContent = "checking…";
  try {
    DATA.version = await (await fetch("/api/version")).json();
    DATA.mpb_update = await (await fetch("/api/update")).json();
  } catch(e){ /* renderChips will show the failed state */ }
  renderChips();
};

const INTRO_KEY = "mpb.seenIntro";
load(false).then(() => {
  // First visit only, and only once the palace actually loaded — an intro over
  // a failed load explains vocabulary to someone staring at an error.
  if(DATA && !localStorage.getItem(INTRO_KEY)){
    localStorage.setItem(INTRO_KEY, "1");
    vocabSheet();
  }
});
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
    print(f"  browser   : MemPalace Browser {MPB_VERSION}  build {BUILD_ID}")
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
