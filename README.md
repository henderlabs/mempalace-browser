# MemPalace Browser

> A fast, read-only web browser for your private [MemPalace](https://github.com/MemPalace/mempalace).

MemPalace stores your AI's memory as drawers, filed into rooms and wings. It is excellent at
letting an agent retrieve them and offers no way for *you* to look. This is one page that shows
you what is actually in there: browse the wings, read the drawers, search the lot, and see how
much disk it is using.

One file, standard library only, no database of its own, no build step.

> **Unofficial.** This is a community tool from [HenderLabs](https://henderlabs.com). It is not
> affiliated with, endorsed by, or maintained by the MemPalace project. Bugs here are ours — please
> report them [in this repo](https://github.com/henderlabs/mempalace-browser/issues), not upstream.

## Status

Working and in daily use. Best-effort maintenance: issues and PRs are welcome, but no support or
response time is promised. See [Contributing](#contributing).

## Requirements

- MemPalace installed and a palace with drawers in it
- Python 3.9+ (whatever your MemPalace runs on)
- Nothing else. No pip install, no Node, no database.

## Quick start

```bash
git clone https://github.com/henderlabs/mempalace-browser.git
cd mempalace-browser
./run.sh
```

Open <http://127.0.0.1:8080/>. `Ctrl-C` stops it.

`run.sh` finds the right Python by reading the shebang of your `mempalace` command, so it works
whether you installed with `uv tool`, `pipx`, `pip`, or a venv. If it cannot find one, it tells you
what it tried and how to point it manually.

## Configuration

Everything is optional. Defaults are chosen so that running it with no configuration is safe.

| Variable | Default | What it does |
|----------|---------|--------------|
| `MPB_BIND` | `127.0.0.1` | Interface to bind. **See [Security](#security) before changing this.** |
| `MPB_PORT` | `8080` | Port to listen on |
| `MPB_ALLOWED_HOSTS` | `localhost,127.0.0.1,::1` | Extra `Host` headers to accept, comma-separated. Needed if you reach it by a hostname — e.g. `MPB_ALLOWED_HOSTS=palace.lan` |
| `MEMPALACE_PYTHON` | *(auto-detected)* | Interpreter to use, if auto-detection picks wrong |

The palace itself is located by asking MemPalace, so `MEMPALACE_PALACE_PATH` and your
`config.json` are honored automatically — there is no separate path setting to keep in sync.

## Security

**The default is `127.0.0.1`, and you should think before changing it.**

There is no authentication, because on localhost the operating system is the authentication. Bind
it to `0.0.0.0` and every device on your network can read every drawer — including whatever your
palace holds about your health, your work, your family, and your diary, and including the devices
on your network you have not thought about lately. Web pages that *other* devices visit can reach
it too, not just people sitting at a keyboard.

If you need it from another machine, an SSH tunnel keeps the localhost default intact and needs no
password:

```bash
ssh -L 8080:127.0.0.1:8080 you@your-palace-host
```

Only use `MPB_BIND=0.0.0.0` on a network you would hand your unlocked laptop to.

**`Host` header checking.** "Localhost is the authentication" is only true if a browser cannot be
tricked into treating this server as same-origin. A malicious web page can point its own domain at
`127.0.0.1` (DNS rebinding), and the same-origin policy then protects nothing — it could read your
whole palace through `/api/data`. So requests are rejected unless their `Host` is one you have
allowed. If you reach the browser by a hostname rather than an IP, add it:

```bash
MPB_ALLOWED_HOSTS=palace.lan ./run.sh
```

A rejected request logs the exact variable to set, so a 403 tells you what to do rather than
leaving you guessing.

## Backends

Drawer browsing is backend-agnostic — it goes through MemPalace's own collection API.

| Backend | Browsing | Storage panel |
|---------|----------|---------------|
| `chroma` | yes | yes |
| `sqlite_exact` | yes | yes |
| `pgvector` | yes | no — drawers live on another host |
| `qdrant` | yes | no — drawers live on another host |

On remote backends the storage panel says so rather than reporting misleading local disk numbers.

## Design notes

Three decisions are deliberate, and each one is load-bearing:

**It imports the `mempalace` package.** It does not shell out to the CLI, and it does not read
`chroma.sqlite3` directly. Both couple you to something that moves — a second install drifts out of
version sync, and Chroma's schema is internal (that is what `mempalace migrate` exists for). Running
on MemPalace's own interpreter means the browser cannot disagree with your palace about what is in
it. This is also why there is no `requirements.txt` to give it its own venv: that would recreate the
drift.

**Every read passes `create=False`.** MemPalace's `get_collection` defaults to `create=True`, so a
wrong path does not error — it silently manufactures an empty palace and reports success. The
browser also refuses to start if it reads zero drawers, because that is what a wrong path looks
like. Failing loudly beats a confident, empty page.

**Nothing claims success it did not verify.** If PyPI is unreachable, the version chip says
*"update check failed"* — never "up to date". `/api/health` returns 503 with the real error when the
palace cannot be read. A health check that cannot fail is not a health check.

## What it does not do

- **No writes.** It cannot add, edit, delete, or re-file. Use the MCP tools or the CLI for that.
- **No semantic search.** Search is instant client-side text matching over every drawer. Real
  semantic search would mean loading the embedding model and a slow startup; text matching is
  usually what you want when *you* are the one looking.
- **No graph or 3D view.** If you want those, see
  [memory-palace-web-frontend](https://github.com/tomsalphaclawbot/memory-palace-web-frontend).

## Contributing

Issues and pull requests are welcome. This is a small tool with a deliberately small scope — if a
change adds a dependency or a build step, please open an issue first so we can talk about whether
the simplicity is worth trading.

## License

[MIT License](./LICENSE) — © 2026 HenderLabs

MemPalace is a separate project, also MIT licensed, © 2026 MemPalace Contributors.
