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

import hashlib
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
MPB_VERSION = "0.3.0"          # matches the v0.3.0 tag on the repo below
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

        per_room = defaultdict(list)
        for d in items:
            if isinstance(d.get("chunk_index"), int):
                per_room[d["room"]].append(d["chunk_index"])
        dupe_rooms, gap_rooms = [], []
        for rm, idx in per_room.items():
            if len(set(idx)) != len(idx):
                dupe_rooms.append(rm)
            elif sorted(idx) != list(range(len(idx))):
                gap_rooms.append(rm)
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
                "resources": resource_info(),
                "documents": build_documents(drawers),
                "layers": _layer_counts(drawers),
                "palace_notes": [],
                "mpb_version": MPB_VERSION,
                "build_id": BUILD_ID,
                "repo_url": REPO_URL,
                "upgrade_cmd": _upgrade_hint(),
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
            "resources": resource_info(),
            "documents": build_documents(drawers),
            "layers": _layer_counts(drawers),
            "palace_notes": list(_palace_notes_seen),
            "mpb_version": MPB_VERSION,
            "build_id": BUILD_ID,
            "repo_url": REPO_URL,
            "upgrade_cmd": _upgrade_hint(),
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
            elif path == "/api/version":
                self._send(200, json.dumps(version_info(force=True)), "application/json")
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
                    self._send(200, json.dumps({
                        "ok": True,
                        "status": "attention" if notes else "healthy",
                        "drawers": d["count"],
                        "palace": PALACE_PATH,
                        "warnings": notes,
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
      // rooms, so a room filter cannot mean anything to them.
      view = "drawers";
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
      view = "drawers";
      render();
    };
    div.querySelectorAll(".room").forEach(el => el.onclick = e => {
      e.stopPropagation();
      sel = {wing:el.dataset.w, room:el.dataset.r};
      view = "drawers";
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

  // Host first: "is this machine healthy" frames everything under it. Storage
  // only carries a top gap when something sits above it.
  const resHtml = resourcesHtml();
  document.getElementById("info").innerHTML = resHtml
    + `<div${resHtml ? ' style="margin-top:13px"' : ""}>${storeHtml}</div>` + `
    <div class="st-h" style="margin-top:13px">Drawers filed · 12mo</div>
    <div class="spark">${keys.map(k =>
      `<i class="${counts[k]?"has":""}" style="height:${counts[k]? Math.max(8,counts[k]/max*100):2}%"
          title="${mlab(k)} ${k.slice(0,4)}: ${counts[k]}"></i>`).join("")}</div>
    <div class="spark-x"><span>${mlab(keys[0])}</span><span>${mlab(keys[11])}</span></div>
    <div class="st-h" style="margin-top:13px">Layers</div>
    ${layersHtml()}
    ${aboutHtml()}`;
}

// MemPalace's own repository, hardcoded on purpose. Every other URL here is
// ours to change; this one is a claim about someone else's project, and
// MemPalace's README carries a malware warning about impostor domains passing
// themselves off as it. A configurable "upstream" link is a configurable way
// to send our users somewhere that is not upstream. This is the address their
// README names as official.
const MP_REPO = "https://github.com/MemPalace/mempalace";

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
    ["Tunnels",  L.tunnels_explicit && L.tunnels_explicit.n, null,
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
    <div class="st-note" style="margin-top:7px">${passive} passive tunnel${passive===1?"":"s"}
      — room names shared across wings, derived not stored.</div>${rival}`;
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
      <pre class="doc">${hl(d.content, query)}</pre></div>`;
  wireNav(c);
  c.querySelector("[data-back]").onclick = () => render();
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
      <div class="prev">${hl(prev, query)}</div></div>`;
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
      fixes:[{label:"Upgrade (matched to how MemPalace is installed here)", cmd:DATA.upgrade_cmd || "pip install -U mempalace"}],
      verify:"Restart this browser; the version chip should read 'up to date'.",
      undo:"Reinstall the previous version with the same tool."});

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
  const n = issues.length;
  const plural = n === 1 ? "warning" : "warnings";
  if(bad){
    hc.className = "chip act bad"; hc.textContent = `Needs attention · ${n} ${plural}`;
  } else if(n){
    hc.className = "chip act warn"; hc.textContent = `${n} ${plural}`;
  } else {
    hc.className = "chip act ok"; hc.textContent = "Healthy · 0 warnings";
  }
  hc.title = n ? issues.map(i => "• " + i.title).join("\n")
              : "Checked: embedder identity, knowledge-graph split, disk, "
                + "update check, document chunk ordering. Nothing to report.";

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
  if(view === "documents") renderDocs(); else renderList();
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
document.addEventListener("keydown", e => { if(e.key === "Escape") closeSheet(); });

function systemSheet(){
  const issues = healthIssues();
  const v = DATA.version || {}, s = DATA.storage || {};
  const state = issues.some(i=>i.level==="bad") ? "Needs attention"
              : issues.length ? "Warnings present" : "Healthy";
  const notes = issues.length
    ? issues.map((i, n) => advisoryHtml(i, n)).join("")
    : `<div class="sub" style="margin:0">Nothing to report.</div>`;
  sheet(`
    <h2>System status — ${esc(state)}</h2>
    <p class="sub">What this deployment is reading, and anything wrong with it.
      This browser only reads — it never applies a fix. Every advisory below is
      something you (or an agent you hand it to) can run.</p>
    <h3>Warnings (${issues.length})</h3>${notes}
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
  return `<div class="adv ${i.level==="bad"?"bad":""}">
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
      try { await navigator.clipboard.writeText(txt); b.textContent = "Copied ✓"; }
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
