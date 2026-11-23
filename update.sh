#!/usr/bin/env bash
# MemPalace Browser updater.
#
# Run by a HUMAN. The browser never runs this and never updates itself: it is read-only by design,
# and a program that can replace its own code is a supply-chain risk. The browser only tells you an
# update exists (see "Updates" in the sidebar) and shows the command to run this script.
#
# What it does, in order, stopping at the first problem:
#   1. asks GitHub for the latest release of MemPalace Browser (nothing about you is sent)
#   2. downloads app.py and SHA256SUMS from that release
#   3. checks the download against the published checksum, compiles it, and checks that the version
#      inside it is the version the release claims
#   4. backs up the current app.py next to it (app.py.bak-<version>-<time>)
#   5. swaps the new file in atomically
#   6. restarts the service (only if you gave it a way to) and checks /api/health
#   7. if the health check fails, puts the old file back and restarts again
#
# A release must carry two assets: app.py and SHA256SUMS ("<hex>  app.py").
#
# Usage:
#   update.sh                 show what would change and ask before doing it
#   update.sh --check         only report: current version, latest release, whether an update exists
#   update.sh --yes           do not ask
#   update.sh --unit NAME     restart `systemctl --user restart NAME` afterwards and health-check
#   update.sh --restart-cmd C restart with your own command instead (e.g. 'docker compose restart')
#   update.sh --prerelease    consider pre-releases too
#   update.sh --rollback      put the newest backup back (and restart, if told how)
#   update.sh --force         reinstall even if the release is not newer (never an OLDER one: that also needs --allow-downgrade)
#   update.sh --allow-downgrade  allow installing a release older than the one running
# Environment: MPB_UNIT, MPB_PORT (default 8080), MPB_RELEASES_URL (default: GitHub's latest release API).
set -euo pipefail

REPO="${MPB_REPO:-henderlabs/mempalace-browser}"
API="${MPB_RELEASES_URL:-https://api.github.com/repos/${REPO}/releases/latest}"
PORT="${MPB_PORT:-8080}"
UNIT="${MPB_UNIT:-}"
RESTART_CMD=""
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${DIR}/app.py"
MODE="update"; ASSUME_YES=0; PRERELEASE=0; FORCE=0; ALLOW_DOWN=0; PRE_COUNT=0

die() { echo "update.sh: $*" >&2; exit 1; }
say() { echo "== $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE="check" ;;
    --rollback) MODE="rollback" ;;
    --yes|-y) ASSUME_YES=1 ;;
    --force) FORCE=1 ;;
    --allow-downgrade) ALLOW_DOWN=1 ;;
    --prerelease) PRERELEASE=1 ;;
    --unit) shift; UNIT="${1:-}" ;;
    --restart-cmd) shift; RESTART_CMD="${1:-}" ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

command -v python3 >/dev/null || die "python3 is required"
command -v curl >/dev/null || die "curl is required"
[ -f "$APP" ] || die "cannot find app.py next to this script ($APP)"
if [ -n "$UNIT" ] && [ -z "$RESTART_CMD" ]; then
  case "$UNIT" in *[!A-Za-z0-9@._:-]*) die "unit name has unexpected characters: $UNIT" ;; esac
  RESTART_CMD="systemctl --user restart ${UNIT}"
fi

current_version() { sed -n 's/^MPB_VERSION = "\([^"]*\)".*/\1/p' "$1" | head -n1; }
sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# Sort key: dev/rc/beta builds sort BELOW the release they lead up to (0.4.0-dev.9 < 0.4.0).
vkey() { python3 - "$1" <<'PY'
import re, sys
m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(dev|rc|alpha|beta|a|b)[.\-]?(\d+)?)?", sys.argv[1].strip(), re.I)
print("%05d.%05d.%05d.%d.%05d" % (int(m[1]), int(m[2]), int(m[3] or 0), 0 if m[4] else 1, int(m[5] or 0)) if m else "invalid")
PY
}

health_ok() {
  local i
  for i in $(seq 1 20); do
    if curl -fsS -m 3 -H "Host: localhost" "http://127.0.0.1:${PORT}/api/health" 2>/dev/null | grep -q '"ok": *true'; then return 0; fi
    sleep 1
  done
  return 1
}

health_drawers() {
  curl -fsS -m 3 -H "Host: localhost" "http://127.0.0.1:${PORT}/api/health" 2>/dev/null \
    | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("drawers", 0)))' 2>/dev/null || echo 0
}

# After a restart, "healthy" must also mean "the palace is really there": drawers above zero and not far below what it was before.
verify_count() {
  local n; n="$(health_drawers)"
  [ "${n:-0}" -gt 0 ] || { echo "update.sh: the new version reports 0 drawers" >&2; return 1; }
  if [ "${PRE_COUNT:-0}" -gt 0 ] && [ $((n * 10)) -lt $((PRE_COUNT * 9)) ]; then
    echo "update.sh: drawer count fell from ${PRE_COUNT} to ${n}" >&2; return 1
  fi
  say "Drawers: ${n} (before: ${PRE_COUNT:-unknown})"
}

restart_and_check() {
  if [ -z "$RESTART_CMD" ]; then
    say "Not restarted: no --unit or --restart-cmd given. Restart MemPalace Browser to run the new version."
    return 0
  fi
  say "Restarting: ${RESTART_CMD}"
  bash -c "$RESTART_CMD" || return 1
  say "Waiting for /api/health on port ${PORT}"
  health_ok && verify_count
}

CUR="$(current_version "$APP")"; [ -n "$CUR" ] || die "cannot read MPB_VERSION from $APP"

if [ "$MODE" = "rollback" ]; then
  BAK="$(ls -1t "${APP}".bak-* 2>/dev/null | head -n1 || true)"
  [ -n "$BAK" ] || die "no backup found next to app.py"
  say "Rolling back ${CUR} to $(current_version "$BAK") from $(basename "$BAK")"
  if [ "$ASSUME_YES" != 1 ]; then read -r -p "Proceed? [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || die "cancelled"; fi
  cp -p "$BAK" "${APP}.new" && mv -f "${APP}.new" "$APP"
  restart_and_check || die "restarted but /api/health did not answer; check the service"
  say "Rolled back to $(current_version "$APP")"; exit 0
fi

say "Checking ${API}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
HTTP="$(curl -sSL -m 15 -w '%{http_code}' -H "Accept: application/vnd.github+json" -H "User-Agent: mempalace-browser-update" "$API" -o "$TMP/release.json" 2>/dev/null || true)"
[ "$HTTP" != 404 ] || die "no release is published yet at ${API}"
[ "$HTTP" = 200 ] || die "could not reach the release API (HTTP ${HTTP:-none}; offline?)"

python3 - "$TMP/release.json" "$PRERELEASE" > "$TMP/rel.txt" <<'PY' || die "no usable release found"
import json, sys
d = json.load(open(sys.argv[1])); pre = sys.argv[2] == "1"
rels = d if isinstance(d, list) else [d]
rel = next((r for r in rels if not r.get("draft") and (pre or not r.get("prerelease"))), None)
if not rel: sys.exit(1)
a = {x.get("name"): x.get("browser_download_url") for x in rel.get("assets", [])}
print(rel.get("tag_name", "")); print(a.get("app.py", "")); print(a.get("SHA256SUMS", "")); print(rel.get("html_url", ""))
PY
TAG="$(sed -n 1p "$TMP/rel.txt")"; APP_URL="$(sed -n 2p "$TMP/rel.txt")"; SUM_URL="$(sed -n 3p "$TMP/rel.txt")"; PAGE="$(sed -n 4p "$TMP/rel.txt")"
NEW="${TAG#v}"
[ -n "$TAG" ] || die "release has no tag"
say "Installed: ${CUR}    Latest release: ${NEW}    ${PAGE}"

KC="$(vkey "$CUR")"; KN="$(vkey "$NEW")"
[ "$KC" != invalid ] && [ "$KN" != invalid ] || die "cannot compare versions (${CUR} / ${NEW})"
if [ "$KN" \> "$KC" ]; then STATE="update"; elif [ "$KN" = "$KC" ]; then STATE="same"; else STATE="ahead"; fi
case "$STATE" in
  same)  say "Already on the latest release." ;;
  ahead) say "This copy (${CUR}) is newer than the latest release (${NEW}). Nothing to do." ;;
  update) say "An update is available." ;;
esac
[ "$MODE" = "check" ] && exit 0
if [ "$STATE" != update ] && [ "$FORCE" != 1 ]; then exit 0; fi
if [ "$STATE" = ahead ] && [ "$ALLOW_DOWN" != 1 ]; then
  die "refusing to install release ${NEW} over the newer ${CUR}. If you really mean it, add --allow-downgrade."
fi
say "To undo this later: bash '${DIR}/update.sh' --rollback --yes${UNIT:+ --unit ${UNIT}}"

[ -n "$APP_URL" ] && [ -n "$SUM_URL" ] || die "release ${TAG} must carry app.py and SHA256SUMS assets"
case "$APP_URL$SUM_URL" in https://*https://*|http://127.0.0.1*|http://localhost*) ;; *) die "refusing a non-https download URL" ;; esac

say "Downloading app.py and SHA256SUMS"
curl -fsSL -m 120 "$APP_URL" -o "$TMP/app.py" || die "download of app.py failed"
curl -fsSL -m 30 "$SUM_URL" -o "$TMP/SHA256SUMS" || die "download of SHA256SUMS failed"
WANT="$(awk '$2=="app.py" || $2=="*app.py" {print $1}' "$TMP/SHA256SUMS" | head -n1)"
[ -n "$WANT" ] || die "SHA256SUMS has no entry for app.py"
GOT="$(sha256_of "$TMP/app.py")"
[ "$WANT" = "$GOT" ] || die "checksum mismatch (published ${WANT:0:12}…, downloaded ${GOT:0:12}…). Nothing was changed."
say "Checksum matches (${GOT:0:12}…)"
python3 -m py_compile "$TMP/app.py" || die "downloaded app.py does not compile. Nothing was changed."
INSIDE="$(current_version "$TMP/app.py")"
[ "$INSIDE" = "$NEW" ] || die "release says ${NEW} but app.py says '${INSIDE}'. Nothing was changed."

if [ "$ASSUME_YES" != 1 ]; then
  read -r -p "Replace ${CUR} with ${NEW}? A backup is kept. [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || die "cancelled"
fi

BAK="${APP}.bak-${CUR}-$(date +%Y%m%d-%H%M%S)"
[ -n "$RESTART_CMD" ] && PRE_COUNT="$(health_drawers)"
cp -p "$APP" "$BAK"; say "Backup: $(basename "$BAK")"
cp "$TMP/app.py" "${APP}.new"; chmod --reference="$APP" "${APP}.new" 2>/dev/null || true
mv -f "${APP}.new" "$APP"; say "Installed ${NEW}"

if ! restart_and_check; then
  echo "update.sh: the new version did not come up healthy. Rolling back." >&2
  cp -p "$BAK" "${APP}.new" && mv -f "${APP}.new" "$APP"
  [ -n "$RESTART_CMD" ] && bash -c "$RESTART_CMD" || true
  die "rolled back to ${CUR}. The failed version was ${NEW}."
fi
say "Done: ${CUR} -> ${NEW}. Open the browser once and look at it before you trust it."
say "The backup stays until you remove it: $(basename "$BAK"). Rollback any time with: bash '${DIR}/update.sh' --rollback"
