# Connections: one install, several homeservers

A **connection** is one gateway this install's agent is enrolled at, served by one
remote-gateway bridge instance. The directory of connections lives at
`<workspace>/state/connections.json`, and `src/connections.py` is its only writer:
the CLI (`scripts/connections.py`), the runtime API (`connection.*` methods) and
startup (`src/startup-runtime.sh`) all call that module and never touch the file.

## The model

| Field | Meaning |
|---|---|
| `server` | The homeserver the connection belongs to (`s.space`), never the gateway hostname (`chat.s.space` and `matrix.s.space` are endpoints of that server). |
| `instance` | The bridge instance slug (`s-space`): `GATEWAY_INSTANCE`, the suffix on `state/gateway-status.<instance>.json`, the `task-<instance>~<broker-id>` id prefix, the `channels/<instance>-ag2space/` config dir. `""` only for the legacy unsuffixed bridge imported from `REMOTE_TASK_TOKEN`. |
| `gateway_url` | Stored exactly as the relay issued it inside the `url|secret` token. Never rewritten: an endpoint rename never touches stored rows. |
| `token_ref` | The vault key the secret lives under (`AG2_REMOTE_TOKEN_<INST>`; `REMOTE_TASK_TOKEN` for the legacy row). The secret itself is never in this file. |
| `primary` | At most one row. Proactive nudges and the owner-DM defaults stay with the primary; every other bridge launches with `REMOTE_PROACTIVE_ROOM` set empty, whatever the parent environment holds. |
| `added_by` | `login`, `owner`, `ui` or `env`. |
| `added_at` | ISO 8601 UTC. |
| `verified` | Whether the server came from the gateway's own answer (below) rather than from the gateway hostname. |
| `server_source` | Where `server` came from: `identity` (the DotSpace the resolver registers for the gateway host, confirmed by the gateway's own agent listing), `resolver` (the registered DotSpace alone), or `gateway` (the host itself, provisional). |
| `agent_mxid` | The agent's identity at that server, when the gateway named it. |
| `agent_display_name`, `agent_avatar_url`, `owner_mxid` | Optional, from the same listing entry as `agent_mxid` and only on a verified preview: the entry's `display_name`, its `avatar_url` (stored only as an `mxc://` URI) and its `owner` mxid. Absent until the gateway sends them; never a secret. The list spells every one of them out, `null` when unknown. |
| `disabled` | `true` after `disconnect`: the bridge is stopped and the launch plan skips the row, token and launch record kept, until `reconnect` or `update_token` clears it. Absent on older rows, which load as enabled. |

The record is validated on every read (schema, bounds, one primary, unique instance /
token_ref / server) and published atomically under a lock file beside it, so a reader
never sees a half-written or forged row.

### Two policies, kept apart

The connections list is the install's **instance directory**: it decides which
bridges startup launches and where each one's state lives. It is *not* an
authorization list. Adding a connection never widens the Sutando Cloud bearer
allowlist, never reads or writes `state/auth/` or anything `cloud_auth` reads, and
grants the new gateway nothing beyond what its own token already carries.
`tests/connections-no-cloud-trust.test.py` pins that.

## Adding: preview, then confirm

Adding is two-step and does not care where the code came from (pasted into the UI,
typed at the CLI, handed over by a browser flow, present in the environment):

1. **Preview.** `preview_connection(token)` first checks the gateway URL and host: the
   URL must be `https`, and the host a multi-label server name under none of the reserved
   suffixes (`.localhost .local .internal .test .example .invalid .onion .home.arpa`),
   never an IP literal or a single label (the resolver's own `is_server_name` and
   never-fetched rules). It then resolves the name **once** and refuses it if any answer
   is loopback, private, link-local, multicast, reserved, unspecified or an IPv4-mapped
   form of those. Any of these is `invalid_gateway` before a single packet, so a token
   cannot aim the probe at the operator's own machine or network; a name that does not
   resolve is `unreachable (resolution)`, also unprobed. The probe itself is **pinned**
   to the vetted address (TCP to that IP, SNI and `Host` carrying the original name), so
   a DNS re-answer between the check and the connect cannot swap the target. It then
   asks the DotSpace resolver which server the host is registered for
   (`dotspace_for(gateway_host)`, source config or well-known) and runs the bridge's own
   authenticated probe (`GET /v1/agents` under the token's bearer, following no redirect).
   The same guard runs on confirm (which previews again server-side) and on `reconnect`'s
   identity learn. **A listing can confirm an identity, never establish one:**
   `verified: true` (`server_source: identity`) only when the gateway names exactly one
   agent whose server is the registered server. A registered host whose listing disagrees
   or is missing stays `resolver`; an unregistered host is the server itself (`gateway`)
   whatever the listing claims. `error` names a refusal: `auth_rejected` (401/403),
   `not_a_gateway` (404), `http_<code>`, or `unreachable (...)`. Nothing is saved.
2. **Confirm.** `add_connection(token, expected_server=<what the preview showed>)`
   runs the preview again and saves only if the identity still matches; a changed
   answer is a `ConnectionMismatch` (the runtime API answers `CONFLICT`, HTTP 409
   `mismatch`, the CLI exits 1). A token the gateway rejected on the probe is never
   stored (`TokenRejected`, 409 `token_rejected`); an unreachable gateway is stored as
   a pending, unverified row. The secret goes to the vault under `token_ref` first; a
   vault key that already holds a secret no row owns is never overwritten
   (`VaultKeyInUse`, 409 `vault_key_in_use`), and if the vault refuses, the add fails
   closed and nothing is written.

Adding is **idempotent per connection** and starts no second bridge (the bridge's
singleton lock is the backstop, not the policy). A row whose server the registry or an
identity vouches for (`resolver`/`identity`) matches a token for the same server, so one
registered homeserver's endpoints (`chat.` and `matrix.`) are one connection. A
provisional (`gateway`) row matches only a token for its own gateway host, so it can
never stand in for a real connection elsewhere: a gateway at another host claiming
someone else's identity gets its own provisional row and the real token is still stored.
A verified preview, or a `reconnect` that learns the identity, **re-keys** a provisional
row to the identity's server in place: one row, one bridge, the instance slug and vault
key unchanged. A row keyed by identity is never demoted.

A saved row is primary only when the install has no primary yet. `import-env` gives the
flag only to the `REMOTE_TASK_TOKEN` row (the legacy unsuffixed bridge), never to a
named `AG2_REMOTE_TOKEN_<INST>`; removing the primary leaves none until `set_primary`
or the next saved row. Adding **never changes which row is primary** except that the very
first row saved becomes primary. `set_primary(instance)` is the one way to move it,
and moving it is what moves proactive and owner-DM routing at the next launch.

## State

`status_for(row)` joins the row with the bridge's own
`state/gateway-status[.<instance>].json` and with the launch record `launch` leaves
under `state/connections-launch/<instance>.json`:

| `state` | When |
|---|---|
| `pending` | Saved; no sidecar yet (bridge not up, or never launched). |
| `connected` | The sidecar says `connected: true`. |
| `disconnected` | The sidecar says `connected: false` for a non-auth reason; or, with `reason: disabled`, the row was disconnected by the owner (whatever a stale sidecar still says). |
| `failed` | With `reason`: `no_token`, `launch_failed`, `auth_rejected` (the sidecar reports a 401/403 or a pending re-enrol). |

`disconnect(instance)` stops the one bridge holding that instance's singleton lock (the
same stop `remove_connection` and `reconnect` use) and marks the row `disabled`; the row,
its vault key and its launch record stay, so the owner can come back without a new token.
The launch plan omits a disabled row, so a core restart leaves it down. The primary cannot
be disconnected while it is primary (`PrimaryCannotDisconnect`, HTTP 409
`primary_cannot_disconnect`, CLI exit 1): make another row primary first. Disconnecting a
disabled row is a no-op. `reconnect(instance)` clears the flag, stops the bridge if one is
running, learns the identity its token answers to now and launches it again; the other
connections keep running. A plain `launch` of a disabled row is refused by name
("disconnected; reconnect it"). `update_token(instance, token)` is for a code that was
rotated or revoked on the homeserver (Get new token, Revoke) or an agent moved away and
back: it previews the new code exactly as `add` does, refuses one for another server
(`TokenServerMismatch`, code `token_server_mismatch`) or for another agent while the row
names one (`TokenAgentMismatch`, `token_agent_mismatch`; a code for another agent is a new
connection, never pasted over this row) or one the gateway rejected (`token_rejected`), with
the vault and the row untouched; otherwise it replaces the secret under the row's existing
`token_ref`, keeps the instance, primary flag, `added_at` and launch record, updates
`gateway_url` and the identity fields from the preview (a row without an agent identity learns
one), clears `disabled` and relaunches the bridge the way `reconnect` does, so a disconnected
or failed row comes back on the new code; a relaunch that fails is the row's `failed` state,
not an error of the update. `remove_connection(instance)`
stops that bridge the same way, then drops the row, its vault key and its launch record,
and nothing else; the primary needs `force`, and no remaining row inherits the flag. A
removed env-imported row is remembered in the record's `removed_env` list, so
`import-env` does not bring it back while the variable is still set (it says
"connection removed earlier; unset the variable"); an explicit add of that key clears
the tombstone.

## Startup

`start_gateway_lanes` (in `src/startup-runtime.sh`) first runs `import-env`, which
creates a row once for `REMOTE_TASK_TOKEN` and each `AG2_REMOTE_TOKEN_<INST>` in the
environment (copying the secret into the vault unless one is already there; never
overwriting a row), then launches one bridge per `launch-plan` line through
`scripts/connections.py launch <instance>`. The plan lists every enabled row with an
instance (a disabled row stays down, and its env token is not re-planned either) plus any
env token the import could not record, so an unavailable vault degrades to the previous
behaviour instead of losing a gateway. The plan is startup's only source of what to launch,
so `disconnect` needs nothing from the shell. An unreadable record never reads
as "no secondaries": `launch-plan` exits non-zero naming the parse error, startup prints
a ⚠ line and launches each `AG2_REMOTE_TOKEN_<INST>` with `launch --env-only`, from the
variable alone. The legacy unsuffixed bridge keeps its own launch path.

The bridge environment (`GATEWAY_INSTANCE`, `REMOTE_TASK_TOKEN` from the env var of
the same name or the vault, empty `REMOTE_TASK_URL`, `REMOTE_TASK_CHANNEL_DIR`,
`REMOTE_PROACTIVE_ROOM` only for the primary, `SUTANDO_SUPERVISED=1`) is composed in
`connections.launch_env`, nowhere else. The legacy unsuffixed bridge keeps both of its launch
paths (the launchd wrapper, and the bare launch when no job is installed); each asks
`connections.py primary` before every launch, and once a named row holds the primary launches
with `REMOTE_PROACTIVE_ROOM` empty, so exactly one bridge posts nudges. Otherwise the variable
passes through untouched, unset included: the bridge reads the desktop's device env only when it
is unset, so an exported empty value would silence nudges on a plain install.

## Known servers

`known_servers()` is the Add picker's offer: every DotSpace the resolver registers
(`dotspace.registered_instances()`: the configured `core.dotspace.instances` list, else
the built-in default; `src/dotspace.py` is the one place those names live), one entry per
server in registry order, joined read-only with the rows:
`{"server", "connected": <a row exists>, "instance": <that row's slug or null>, "primary", "disabled"}`.
Nothing is fetched and no vault key name is in the answer. A server with no row is what the
picker offers; one with a row is already in the directory (and, when `disabled`, resumes
through reconnect rather than a new add). A row for an unregistered server is in the list,
not here.

## Surfaces

- CLI: `scripts/connections.py list | known-servers | add | set-primary | remove | disconnect | reconnect | update-token | launch | import-env | launch-plan`.
- Runtime API (Unix socket, JSON-RPC): `connection.list`, `connection.known_servers`, `connection.add` (`{token}` previews; `{token, confirm: true, expected_server}` saves), `connection.remove`, `connection.set_primary`, `connection.disconnect`, `connection.reconnect`, `connection.update_token` (`{instance, token}`; another server or agent is `CONFLICT`).
- HTTP (`src/agent-api.py` on `127.0.0.1:7843`, the web client's transport; routes dispatch to the same units):

  | Route | Body | Answer |
  |---|---|---|
  | `GET /api/v1/connections` | — | 200 `{"connections": [rows joined with status, state, server_source, disabled, identity fields; no secrets]}` |
  | `GET /api/v1/connections/known-servers` | — | 200 `{"servers": [{"server", "connected", "instance", "primary", "disabled"}]}`; matched before any `<instance>` route (405 for another method, never a slug lookup) |
  | `POST /api/v1/connections` | `{"token", "confirm": false}` | 200 `{"preview": {...}}` |
  | `POST /api/v1/connections` | `{"token", "confirm": true, "expected_server", "added_by": "ui"}` | 201 `{"created": true, "connection": row}`; 200 `{"created": false, ...}` for an existing row; 409 `{"error": "mismatch", "server_from_token", "expected_server"}`; 400 `{"error": ...}`; 503 `{"error": "vault_unavailable"}` |
  | `DELETE /api/v1/connections/<instance>[?force=1]` | — | 204; 409 `{"error": "primary"}`; 404 |
  | `POST /api/v1/connections/<instance>/primary` | `{}` | 200 `{"connection": row}` |
  | `POST /api/v1/connections/<instance>/disconnect` | `{}` | 200 `{"connection": row}` (`disabled: true`, state `disconnected`); 409 `{"error": "primary_cannot_disconnect"}`; 404 |
  | `POST /api/v1/connections/<instance>/reconnect` | `{}` | 202 `{"launch": record}`; clears `disabled` |
  | `POST /api/v1/connections/<instance>/token` | `{"token"}` | 200 `{"connection": row}` (secret replaced under the same `token_ref`, `disabled` cleared, bridge relaunched); 409 `{"error": "token_server_mismatch"}` or `{"error": "token_agent_mismatch"}` (nothing changed); 409 `{"error": "token_rejected"}`; 400 without a token; 404 |

### Authorization for the HTTP routes

  Every connections route, GET included, is behind one gate (`src/local_api_gate.py`,
  the same module the dashboard's mutation gate lives in), checked in this order:

  1. **Host** must name loopback (`127.0.0.1`, `localhost`, `[::1]`) on the service's
     bound port — a DNS-rebinding page puts another name here — else 403.
  2. **Origin** must be absent (a local tool, a same-origin proxy) or loopback, else 403.
  3. **Bearer** `Authorization: Bearer <token>` must match, always: `SUTANDO_API_TOKEN`
     when the operator configured one, else the per-install secret the service mints
     once into `<workspace>/state/agent-api.token` (0600, atomic). No bearer is 401 on
     all six routes, with no `SUTANDO_API_TOKEN` set as much as with one.

  Responses carry `Access-Control-Allow-Origin` only for a loopback Origin whose request
  passed the whole gate (echoed, with `Vary: Origin`), never `*`, and never on a
  refusal; the preflight grants a loopback Origin `GET, POST, DELETE, OPTIONS` and
  `Content-Type, Authorization` and grants nothing else. `check_auth` (the `POST /task`
  gate, which is open when `SUTANDO_API_TOKEN` is unset) is **not** enough here: a
  reconnected bridge runs tasks at the local tier cap, which defaults to owner, so a
  page that could add or reconnect a connection would run owner-tier tasks under an
  attacker's token. The web client must present the bearer; the desktop shell reads
  `state/agent-api.token` and supplies it, and until that ships the UI shows "needs
  the desktop update" instead of calling these routes.

`instance_for` keeps a slug within the bridge's 32-character limit by appending a short
digest to a long server name, so two long names never share a slug.
