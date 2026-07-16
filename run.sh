#!/usr/bin/env bash
# MemPalace Browser — on-demand launcher.
#
# Finds an interpreter that can import mempalace, whatever way you installed it.
#
# The browser deliberately runs on MemPalace's OWN interpreter and imports the
# mempalace package, rather than shelling out to the CLI or reading chroma.sqlite3
# directly. Both of those couple you to something that moves: a second install
# drifts out of version sync, and Chroma's schema is internal (that is what
# `mempalace migrate` is for). Importing the package is why this cannot drift.
#
# Do NOT "fix" this by giving the browser its own venv with mempalace as a
# dependency. That is the two-install skew this design exists to avoid.

set -euo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/app.py"

find_python() {
  # 1. Explicit override always wins.
  if [[ -n "${MEMPALACE_PYTHON:-}" ]]; then
    echo "$MEMPALACE_PYTHON"; return
  fi

  # 2. The mempalace console script's shebang points at an interpreter that can
  #    import mempalace — by construction, whatever installed it. This covers
  #    uv tool, pipx, pip --user, and plain venvs in one shot.
  local cli shebang
  if cli="$(command -v mempalace 2>/dev/null)"; then
    shebang="$(head -1 "$cli" 2>/dev/null || true)"
    if [[ "$shebang" == '#!'* ]]; then
      local interp="${shebang#\#!}"
      interp="${interp%% *}"                       # drop args, e.g. `python -E`
      if [[ "$interp" == */env ]]; then interp=""; fi   # `#!/usr/bin/env python` — no path
      if [[ -n "$interp" && -x "$interp" ]]; then
        echo "$interp"; return
      fi
    fi
  fi

  # 3. Whatever python is on PATH, if it happens to have mempalace (covers an
  #    activated venv, and `#!/usr/bin/env python` installs from step 2).
  local p
  for p in python3 python; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import mempalace" 2>/dev/null; then
      command -v "$p"; return
    fi
  done

  # 4. Known install locations, as a last resort.
  for p in \
    "$HOME/.local/share/uv/tools/mempalace/bin/python" \
    "$HOME/.local/share/pipx/venvs/mempalace/bin/python" \
    "$HOME/.local/pipx/venvs/mempalace/bin/python"
  do
    if [[ -x "$p" ]] && "$p" -c "import mempalace" 2>/dev/null; then
      echo "$p"; return
    fi
  done

  return 1
}

if ! PY="$(find_python)"; then
  cat >&2 <<'EOF'
ERROR: could not find a Python interpreter with MemPalace installed.

The browser imports the mempalace package directly, so it needs the same
interpreter your MemPalace uses. Tried, in order:
  1. $MEMPALACE_PYTHON
  2. the shebang of your `mempalace` command
  3. python3 / python on PATH
  4. common uv-tool and pipx install locations

Fix it either way:
  - make sure `mempalace --version` works in this shell, or
  - point at the interpreter yourself:
      MEMPALACE_PYTHON=/path/to/python ./run.sh

Verify a candidate with:
      /path/to/python -c "import mempalace; print(mempalace.version.__version__)"
EOF
  exit 1
fi

# -u: unbuffered. Without it Python block-buffers stdout when redirected to a
# file or pipe, so the startup banner never appears until the process exits.
exec "$PY" -u "$APP" "$@"
