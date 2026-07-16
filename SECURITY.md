# Security Policy

## Reporting a vulnerability

Please report security issues **privately** via
[GitHub Private Vulnerability Reporting](https://github.com/henderlabs/mempalace-browser/security/advisories/new)
rather than opening a public issue. This tool reads personal memory, so a public
report is a public exploit until it is fixed.

Expect an acknowledgement within a week. This is a best-effort project maintained
by one person — see the README.

**Found something in MemPalace itself, not this browser?** Report it to
[the MemPalace project's own security process](https://github.com/MemPalace/mempalace/security),
not here. We are an unaffiliated companion tool.

## Security model

Understanding this makes it obvious what does and does not count as a vulnerability.

**There is no authentication, by design.** The browser binds `127.0.0.1` and
relies on the operating system: if you can reach localhost on this machine, you
can already read the palace files directly. Authentication would add a password
to protect data that the same user already owns.

**That model has one real weakness, and it is defended.** "Localhost is the
authentication" is only true while a browser cannot be tricked into treating the
server as same-origin. A malicious page can point its own domain at `127.0.0.1`
(DNS rebinding), and the same-origin policy would then let it read `/api/data`.
The browser therefore rejects any request whose `Host` header it does not
recognise. Add your own hostname with `MPB_ALLOWED_HOSTS`.

**`Host` checking is not network access control.** It stops browsers, which
cannot forge `Host`. It does not stop anyone who can reach the port with `curl`.
`MPB_BIND` is the access control.

**`MPB_BIND=0.0.0.0` is an unauthenticated read of your entire palace to the
whole network.** That is documented, not a vulnerability. Do not report it as one.

**Read-only.** Every collection is opened with `create=False`; no `POST`, `PUT`,
`PATCH`, or `DELETE` handler exists. A path that writes to or mutates a palace
*is* a bug, and a serious one — please report it.

**The one outbound request** is a version check against a fixed `pypi.org` URL.
It sends no palace data. Disable it with `MPB_CHECK_UPDATES=0`. Any other
outbound traffic is a bug — please report it.

## In scope

- Any read of drawer data by something that should not have it
- Host allow-list bypasses
- XSS via drawer content, metadata, or the `Host` header
- Any write path to the palace
- Any outbound request other than the documented PyPI check

## Not in scope

- Exposure caused by deliberately setting `MPB_BIND=0.0.0.0`
- Lack of authentication on localhost
- Anything requiring local shell access as the user who owns the palace
