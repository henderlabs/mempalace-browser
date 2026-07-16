# Contributing

Thanks for looking. Issues and pull requests are welcome.

## Before a big change, open an issue

The scope is deliberately small: **one file, standard library only, read-only**.
That is the feature, not a stage it will grow out of. If a change adds a
dependency, a build step, or a write path, please open an issue first — not
because the idea is unwelcome, but because the trade is worth discussing before
you spend an evening on it.

Things that will be turned down without discussion:

- Adding a web framework, a bundler, or a `requirements.txt`
- Reading `chroma.sqlite3` directly instead of going through the `mempalace`
  package (see [Design notes](README.md#design-notes) — this is the whole point)
- Any code path that can write to a palace
- Telemetry or analytics of any kind

## Running it

```bash
./run.sh                 # against your own palace
MPB_DEMO=1 ./run.sh      # synthetic data; needs no MemPalace at all
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

They run the real server in demo mode, so they need no MemPalace, no vector
database, and no palace. If you add a test that needs any of those, that is a
signal something has drifted.

Please add a test for anything touching the `Host` allow-list, HTML escaping, or
the read-only guarantee. Those are the parts where a quiet mistake is expensive.

## Style

Match what is there. No enforced formatter — the codebase is small enough that
consistency is a matter of reading the file first.

Comments should say why, not what. The existing ones exist because something
non-obvious bit us; keep that bar.

## Commits

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`). Bodies are welcome and
encouraged: explain what was wrong, why the fix is right, and what you verified.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
