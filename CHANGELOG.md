# MemPalace Browser — changelog

## [0.4.1] — 2026-11-24

### Changed
- **Richer demo.** `MPB_DEMO=1` now shows 150 synthetic drawers in four wings and eleven rooms (it was nine drawers), so the Galaxy has something to show: clusters that overlap, 20 drawers that sit between two topics, 56 short daily-log entries that fill the space between wings, and three drawers that say they replace an older one. Still synthetic and deterministic; real vectors are never shown in a demo.
- **Docs.** README screenshot replaced with the 0.4 interface. `SECURITY.md` and the pull-request template now name both outbound version checks (PyPI and the GitHub release check). The CI matrix runs on `ubuntu-24.04` so the Python 3.9 job can run.

No change to the browser's behaviour on a real palace.

Previous release: 0.4.0 (below).

## [0.4.0] — 2026-11-23

### Added
- **Supersede chain.** Select a drawer and the map draws the chain of drawers that replaced it or that it replaced, with dates and a clickable list; the drawer's detail view shows the same chain. Links come from what the drawers themselves say ("supersedes the 2026-10-12 wrap") plus meaning and date, and are marked as inferred.
- **Re-file planner.** A read-only audit view that suggests, with reasons and confidence, where drawers should be filed (neighbours by meaning, topic words, spelling, small rooms) and exports the plan as JSON for an agent. The browser never applies it.
- **Update check and updater.** The browser tells you when a newer release exists (Updates row + info finding). It never installs anything: `update.sh`, run by you, verifies a published checksum, keeps a backup, restarts and rolls back on failure. New env: `MPB_RELEASES_URL`, `MPB_UPDATE_CHANNEL`; `MPB_CHECK_UPDATES=0` now turns off both the PyPI and the GitHub check.
- **Staleness overlay.** Toolbar toggle that fades drawers not filed or rewritten lately, using the revision log (a wing move does not count as a rewrite). The tooltip shows how many times a drawer's text was rewritten.
- **Cluster summary.** Hover a dot in a dense one-room pile to see how many drawers the room holds and the span of dates; press L to list the room by date. The map layout is unchanged.
- **Possible conflicts.** Galaxy toolbar button listing very close drawer pairs that state the same kind of value (date, version, address, count, amount) differently, labelled "possible conflict — values differ"; dated snapshots set aside. Heuristic, never a verdict.
- **Trust panel.** New sidebar blocks, all read-only:
  - *This service*: host, systemd unit (or a "started by hand" warning), uptime, listen address, pid.
  - *Trust*: SQLite integrity from MemPalace's own read-only probe (`repair.sqlite_integrity_status`, cached 5 min) and the age of the newest backup.
  - Backups are found with `MPB_BACKUP_GLOBS` (stat only, never opened); a folder and its tarball count as one backup. Unset, the panel says "unknown", it does not guess.
  - New findings, each with a fix command: backup older than 7 days (warning) or 30 days (bad), no backup found, integrity check failed.
- **Secret-looking text flags.** Read-only scan of drawer text for credential shapes: AWS, GitHub, Slack, `sk-`, Google, Notion, Proxmox API tokens, private-key headers, JWT, Bearer, passwords in URLs, and `label: value` pairs (password, token, secret, api key, access key). Findings are masked before they leave the server; a `secret?` badge marks affected drawers; the warning explains that editing a drawer does not remove old text (revision log, SQLite pages, backups) and that the credential must be rotated first. `MPB_SECRET_ALLOW` skips known-good drawers; `MPB_SECRET_SCAN=0` turns it off.
- **Palace audit view.** An *Organization* block (overall, rooms, naming, tunnels, hallways, knowledge graph) and a full audit sheet, from `mempalace.palace_audit.audit_palace` imported as a library function, so it cannot disagree with `mempalace audit`. Findings use the same advisory layout, with look/fix commands and "Copy for your agent". Wing names are placed in commands as quoted data.
- **Galaxy view.** A third view (Drawers · Documents · Galaxy): every drawer is a dot, placed by what it *means*. The server sends each drawer's stored embedding vector (`/api/galaxy`, read through the collection API; no model is loaded, nothing is written) and the browser joins each drawer to its nearest neighbours and settles a force layout, deterministically, so the map is the same every time. Colour by wing, age or author; picking a wing in the sidebar colours its rooms. Hover for author, filed and edited dates; click to open the drawer; search lights matches with a faint halo on their neighbours; small wings get larger, ringed dots and every wing is labelled where there is room. **Changed since** (date, 24 h, 7 d, 30 d, or `?since=YYYY-MM-DD`) rings drawers that are new (white) or edited (amber); **Play history** replays filing order and pulses drawers when they were edited. Secret-flagged drawers glow red (the *health* overlay). **Privacy** (button or the P key) hides titles, labels, legend and tooltips. Demo mode (`MPB_DEMO=1`) uses synthetic vectors, never real ones.
- **Main page split.** The default Drawers view is now two columns: the latest entries on the left and a compact Galaxy on the right (stacked away below 1180 px wide). Hovering an entry rings its dot on the map. A compact toolbar (colour, 24 h, 7 d, clear, privacy, expand) opens the full-screen Galaxy.
- **Entries expand in place.** On the main page each entry in the list is an accordion: click (or Enter/Space) to expand it to its full text with filed/author/edited/size, and three actions: *Open full view · history*, *Show on map* (centres and zooms the Galaxy on its dot) and *Collapse*. Expanded state survives searches and filters; *expand first 50* and *collapse all* sit beside the count. Selecting text inside an expanded entry does not collapse it. The map toolbar gained a *fit* button.
- **Drawer history (supersede chain).** Opening a drawer now shows its History: the current version marked plainly, then each earlier version (when it was superseded, size, and a preview), read through `mempalace.revisions.load_drawer_revisions` (no file is read directly). Previews are masked with the secret-scan rules, because an old version can hold a credential that a later edit removed. Revisions are keyed per parent drawer, so a chunk shows the whole drawer's history. A drawer opened from the list now opens in the left pane, so the map stays beside it with the drawer's dot ringed.
- **Secrets in old versions.** The Secrets row and a new warning report credential-shaped text that survives only in the revision log (the current drawers are clean). Editing or redacting a drawer does not remove it; the warning says so and says to rotate first.
- `/api/health` now includes `integrity` and treats an integrity failure as a warning.
- Advisories have a third level, `info`, for things that are not faults.

### Fixed
- The numeric bot id and allowed-user id in a chat-bot token note are masked in previews; the L key is documented as not working while the search box has focus; a search limited to one wing/room says so, with a link to search all wings.
- Previews, tooltips and titles mask credential-shaped identifiers; Copy plan works over plain http; Play history has a way back to today; the 30 d button is in every toolbar; one author is one colour; same-day successive runs are one conflict entry.
- "Document filed more than once" no longer fires for several drawers from one source; it needs a repeated chunk number inside one drawer.
- Notes (info) are not counted as warnings in the status chip.
- Wing labels on the Galaxy never overlap each other, the legend or the key; every wing keeps a label (moved with a leader line when its centre is crowded).
- A drawer move or re-chunk is no longer counted as a rewrite (it writes a revision whose text equals the current text).
- Play history timeline includes edits newer than the latest filing.
- Superseded versions in a drawer's History can be expanded (masked, capped at 6,000 characters).
- Month labels in *Drawers filed* are correct west of UTC (were one month early).
- The Tunnels row and its footer no longer disagree (3 set; the rest implied by room names are labelled as not counted).
- Token IDs (`user@realm!name`) are listed as an info finding (names, not secrets), with a command to check whether the token is still in use.
- *Changed since* rewritten counts are monotonic and use real text rewrites (revision log), not moves (a drawer both new and edited in the window is counted in both numbers).

### Changed
- The right sidebar is folding: one status line per section, opens in place, problems open themselves, remembered per browser.
- *Show on map* (and opening an entry) selects the drawer: reserved colour, neighbours lit with lines, everything else dimmed; Esc, fit or a click on empty map leaves the state.
- Galaxy has a key for its rings and legends for age and author colour.
- The health chip's tooltip lists the new checks (SQLite integrity, backup age, secret-looking text).
- Sidebar order: This service, Trust, Organization, Host, Storage, Drawers filed, Layers.

### New environment variables
| Variable | Default | What it does |
|---|---|---|
| `MPB_BACKUP_GLOBS` | *(unset)* | Comma-separated globs of your backups; enables the "Last backup" row and backup findings |
| `MPB_SECRET_SCAN` | `1` | `0` turns the secret-text scan off |
| `MPB_SECRET_ALLOW` | *(empty)* | Comma-separated drawer ids to stop flagging |
| `MPB_RESOURCES` | `0` | (existing) host CPU and memory; enable with `MPB_RESOURCES=1` |

### Compatibility
- Built and verified on MemPalace 3.12.0 (2026-11-22). The integrity probe and the audit are importable package functions but not documented public APIs, so other MemPalace versions may differ. The README states the tested version.
