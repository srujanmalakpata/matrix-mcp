# matrix-mcp (hub fork)

A cut-down, hardened fork of [mindroom-ai/matrix-mcp](https://github.com/mindroom-ai/matrix-mcp)
v0.9.1 (MIT), for one job: let Claude Code read a self-hosted, unencrypted Matrix homeserver whose
rooms are bridged from other chat networks, and write in it only when its user asks.

Messages in those rooms come from strangers. So this fork removes everything that isn't needed
for that job, and fixes the problems a security review of v0.9.1 found (the review lives with the
deployment plan, not in this repo).

## What it does

Fourteen tools, all taking raw Matrix IDs (`!room`, `$event`, `@user`):

| Read (9) | Write (5) |
| --- | --- |
| `matrix_whoami`, `matrix_list_rooms`, `matrix_get_room_info`, `matrix_list_room_members`, `matrix_get_profile`, `matrix_read_history`, `matrix_get_event_context`, `matrix_read_thread`, `matrix_get_unread` | `matrix_send_message` (plain text only), `matrix_reply`, `matrix_react`, `matrix_edit_message` (own messages), `matrix_mark_read` (private receipt by default) |

Text written by other people (message bodies, room names, topics, display names, file names) is
labelled as untrusted in every output schema and in the server instructions.

## What changed from upstream v0.9.1

- **Removed:** sending local files (`file_path`), media upload, download and send, numeric room and
  event references, redaction, joining, leaving, creating rooms, invitations, user search, room and
  profile changes, end-to-end encryption, the hosted HTTP/OAuth mode, SSO login, HTTP header
  commands, and the `matrix-nio` / `mindroom-nio` dependency.
- **Every request goes through one bounded HTTP layer:** no redirects, no proxy settings from the
  environment, 2 MiB response cap, bounded retries, a 60-second deadline per call, server error
  text never passed through. A send that gets no response says it may have been sent and gives the
  transaction ID that makes a retry safe.
- **Fields copied from events must look like what they claim** (event IDs, `mxc://` URLs, MIME
  types, msgtypes), or they come back null.
- **Homeserver URL:** https only, except plain http to localhost or a single-label container name.
- **Credentials:** written 0600 in a 0700 folder; the server refuses to load a token file that is a
  symlink, owned by someone else, or readable by others, or one in a folder others can write to;
  it reads through one no-follow descriptor, capped at 64 KiB. Config errors name the bad field
  but never echo its value. Tokens and passwords are prompted for and can't be passed as flags.
  `auth logout` revokes the token on the homeserver before deleting it.
- **Runtime:** stdio only, no FastMCP banner, no PyPI update check, and no `.env` file read from
  the working directory.
- **Supply chain:** fresh lock (OSV-clean when made), `fastmcp>=3.4.10,<4`, pinned build backend,
  CI actions pinned by commit SHA, an OSV check in CI, and a Dockerfile that builds from source
  with uv's locked hashes. Nothing is published to PyPI or a registry.

## Use

```bash
uv sync --locked
uv run matrix-mcp auth password https://hub.example.ts.net alice   # prompts for the password
claude mcp add matrix -- /path/to/matrix-mcp/.venv/bin/matrix-mcp serve
```

`matrix-mcp auth token HOMESERVER USER_ID DEVICE_ID` stores an existing token (prompted for).
`matrix-mcp auth logout` revokes and removes it; `--local-only` removes it even when the server
can't be reached.

## Development

```bash
uv sync --locked --extra dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests && uv run ty check
python3 -I .github/scripts/osv_check.py uv.lock
```

## License

MIT, as upstream. Copyright notices for the original work are in [LICENSE](LICENSE).
