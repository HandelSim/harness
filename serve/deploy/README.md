# Exposing `harness serve`

`harness serve` binds 127.0.0.1 only: the pub page on port N (default 8780)
and opencode web on N+1. Reaching it from elsewhere is up to you. First, the
security truth:

**An opencode web login is a shell on this host, as the user running
`harness serve`.** opencode web has a terminal ("New terminal", ctrl+`), and
its agent runs commands. If that user can `sudo` or reach the docker socket,
everyone who gets in is effectively root. Access codes decide *who* gets that
shell; they do not make it smaller. Run serve as a dedicated, unprivileged
user if more than you will use it.

## 1. ssh -L (one person, nothing exposed)

```
ssh -L 8780:127.0.0.1:8780 -L 8781:127.0.0.1:8781 you@host
```

Open http://127.0.0.1:8780/. Keep the local ports the same as the remote
ones: pub.py checks the Host header, and its opencode link points at port
N+1. No access codes needed; ssh is the gate.

## 2. tailscale serve (your tailnet)

`tailscale serve` can put the two ports on the machine's tailnet name over
HTTPS (check `tailscale serve --help` for your version's syntax). Pass that
name to `harness serve --allow-host <name>` (as the browser sends it,
host:port if not 443). Everyone on the tailnet with access to the node can
reach it, so either use tailnet ACLs as the gate or add access codes
(`harness serve code add NAME`, and `--secure-cookie`) as in option 3.

## 3. Caddy + access codes (a small team, public names)

See `Caddyfile.example`: TLS for two names, the pub page on one, opencode
web on the other behind Caddy's `forward_auth` to pub.py's `/_auth`, with
Caddy adding opencode's Basic login upstream so no token ever sits in a
link. Validate the file offline:

```
caddy adapt --config Caddyfile --adapter caddyfile
```

Codes: `harness serve code add NAME` prints a code once (only its sha256 is
stored); `harness serve code remove NAME` revokes it and ends that name's
sessions on their next request. Sessions are in memory (12 h idle), so a
restart signs everyone out. Once the codes file exists the page stays
gated, even after the last code is removed (then nobody can sign in);
delete `state/serve/access-codes` to open it again. The first `code add`
on a server that started without codes needs a `harness serve` restart.
Failed sign-ins are rate-limited per client (with `--secure-cookie`, by the
last `X-Forwarded-For` entry, which Caddy sets) and overall; enough failures
from anyone pause all new sign-ins for up to 10 minutes, while existing
sessions keep working.

Notes:

- pub.py must accept both names: `--allow-host` for each (Caddy passes the
  client's Host through, to `/_auth` too).
- Each name has its own `/login`; the session cookie is host-only, so a user
  signs in once per name.
- `/pub/` pages also get a second cookie, `SameSite=None`, scoped to
  `/pub/`: the pages run sandboxed with an opaque origin, so their own images
  and links count as cross-site and the main `SameSite=Strict` cookie is not
  sent with them.
- The opencode password is `state/serve/opencode-password`; Caddy needs
  `HARNESS_OPENCODE_BASIC=$(printf 'opencode:%s' "$(cat …)" | base64 -w0)`
  in its environment (systemd `EnvironmentFile=` with mode 600).
