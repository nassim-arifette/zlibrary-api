# CLI output for scripts and agents

This page is the contract of `zlib-cli` for programs that run it: what goes to stdout and stderr,
the JSON shapes, the error object, the error codes, the exit codes and when the CLI may prompt.
For everyday use, see the [user guide](../README.md); for the HTTP server, the
[HTTP API guide](http-api.md).

## Conventions

- **Data goes to stdout.** Prompts, candidate lists, notes, progress, hints and errors go to stderr.
- **`--json`** prints the result as JSON on stdout. It works on every command except `shell`, `serve`,
  `tui` and `mcp`, which print no data on stdout (`serve --json` still makes its errors JSON).
  `check` and `auth-test` always print JSON.
- **JSON is ASCII.** Every non-ASCII character is escaped (`В`), so the bytes are the same on
  every platform and code page. Parse the JSON: the values are unchanged. Each document is one line
  followed by a newline, except batch plans and reports (`download` with a list), which are indented
  with two spaces. `jobs watch --json` prints one event per line (NDJSON).
- **Text output never crashes** on a legacy code page: characters that the stream cannot encode appear
  as backslash escapes. On Windows, set `PYTHONUTF8=1` for UTF-8 pipelines.
- **Unknown keys.** New keys may be added to any object. Ignore the keys you do not know, as with the
  HTTP API's `/v1` contract.
- **Global options work in any position**: `--domain`, `--credentials`, `--timeout`, `--session-file`,
  `--no-session`, `--json`, `--no-input`, `--interactive`, `--config` and `--no-config` are accepted
  before the command, after it and after an action (`favorites add ID:HASH --yes --json`). The last
  occurrence wins. `--no-input` with `--interactive`, `--session-file` with `--no-session`, and
  `--config` with `--no-config`, are refused at any position (exit 2).
- **Settings apply to scripts.** `config.toml` and the `ZLIB_*` variables supply defaults
  ([Settings](reference.md#settings)); pass `--no-config` to ignore the file. Notices about them
  (configured filters, the legacy credentials file, a configured domain that differs from the saved
  session for `status` and `logout`) are one stderr line each, also in JSON mode.
- **`zlib-cli --version`** prints `zlib-cli X.Y.Z` (`zlib-cli unknown` when the package is not
  installed) and exits 0.

## Errors

In JSON mode (the exact token `--json` anywhere before a `--` separator), every failure writes exactly
one JSON object, on one line, to stderr, and nothing to stdout. Usage errors are JSON too.

```json
{"error": "NetworkError", "message": "The request timed out.", "code": "upstream_timeout", "exit_code": 3, "retryable": true, "outcome": "not_applied", "reason": "timeout", "hint": "Run: zlib-cli doctor"}
```

| Key | Type | Present | Meaning |
|---|---|---|---|
| `error` | string | always | The exception type, as printed before this contract (`ValueError`, `AuthenticationError`, ...). New values: `UsageError`, `Cancelled`, `InputRequired`, `InternalError`, `OSError`, `SettingsError`. Prefer `code` |
| `message` | string | always | A literal message. It never contains a password, email, cookie, token or upstream response body |
| `code` | string | always | A stable snake_case code (tables below) |
| `exit_code` | integer | always | The process exit status |
| `retryable` | boolean | always | The same request may succeed later without changes. Advisory: nothing retries on its own |
| `outcome` | string | always | `not_applied`, or `unknown` when an account change or a download may have taken effect |
| `reason` | string | when known | The network or download cause: `dns`, `blocked_address`, `certificate`, `tls`, `timeout`, `redirect`, `http`, `network`, `proxy` (the configured proxy could not be reached or refused the tunnel), or a download reason (`unavailable`, `no_link`, `refused`, `format_mismatch`, `protocol`, `size_limit`, `local_write`, `invalid_book`, `save_failed`, `download`) |
| `quota_may_be_used` | boolean | single-book `download` errors | `true` once the link request was sent: the download may have used quota |
| `hint` | string | optional | A literal next step, in ASCII |
| `key`, `source` | string | settings errors | The setting at fault and where it came from: `cli`, `env` or `config`. The value is never shown |

Without `--json`, the first stderr line is `TypeName: message` and an optional second line starts with
`Hint: `. A cancellation prints `Cancelled.` (or `Cancelled. Nothing was changed.`), and a local I/O
failure prints `Local I/O failed. Check the destination and permissions.`

An unexpected exception is reported as `internal_error` with the message
`Unexpected error. Set ZLIB_DEBUG=1 to show details.` and no traceback. With `ZLIB_DEBUG=1`, the
traceback follows on stderr.

### Partial results

`download` with a list (exit 3 or 4), `doctor` (exit 1 when no domain works) and `jobs watch` (exit 3
or 4) print their data on stdout and exit non-zero without an error object.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, including empty results. `doctor` found a working domain. `jobs watch` saw `succeeded` |
| 1 | Failed: a refusal, a browser challenge, a protocol change, not found, a local I/O or local API problem, an internal error. `doctor` found no working domain |
| 2 | Usage: an invalid argument or value, `input_required`, `session_domain_mismatch`, `config_invalid` |
| 3 | Try again later, nothing to fix: a rate limit or quota, a timeout or network failure on a read, a busy lock (`config_busy`, `config_conflict` included). A list download that only waits. `jobs watch` ended before the task finished |
| 4 | Check before trying again: an account change or a download may have taken effect (`outcome: "unknown"`), a session conflict. List items need attention. `jobs watch` ended on `failed`, `interrupted` or `cancelled` |
| 5 | Sign-in needed: no usable session or credentials, or the session expired or was refused |
| 130 | Cancelled: Ctrl+C, end of input at a prompt, a declined confirmation, a cancelled transfer |

The exit code follows from the error: `cancelled` gives 130; an `unknown` outcome or
`quota_may_be_used` gives 4; then the codes listed for 2, 5, 3 and 4 below; otherwise 3 when
`retryable` is true, else 1.

## Error codes

### Reads and local checks

| Condition | `code` | retryable / outcome | Exit | Hint |
|---|---|---|---|---|
| Command-line usage error | `invalid_argument` | false / not_applied | 2 | `Run: zlib-cli COMMAND --help` |
| Invalid value found by a local check | `invalid_argument` | false / not_applied | 2 | same |
| A prompt would be needed but input is not allowed | `input_required` | false / not_applied | 2 | |
| Saved session for another domain than the one chosen by `--domain`, `ZLIB_DOMAIN`, the config file or a named credentials file (the message names it) | `session_domain_mismatch` | false / not_applied | 2 | `Run zlib-cli login on that domain, or use --no-session.` |
| Invalid config file, key or value (environment or file); a configured folder or credentials file that is missing | `config_invalid` | false / not_applied | 2 | `Run: zlib-cli config unset KEY` (file) or `Fix or unset ZLIB_KEY.` (environment) |
| The config file changed while `config set` or `unset` updated it | `config_conflict` | true / not_applied | 3 | `Run: zlib-cli config list` |
| The config file is locked by another process (5 s) | `config_busy` | true / not_applied | 3 | `Try again in a moment.` |
| Booklist not found | `booklist_not_found` | false / not_applied | 1 | `Run: zlib-cli booklists` |
| `download QUERY` found nothing | `not_found` | false / not_applied | 1 | |
| Sign-in refused, session missing or expired | `upstream_authentication_required` | false / not_applied | 5 | `Run: zlib-cli login` |
| Browser verification required | `upstream_challenge` | false / not_applied | 1 | `Run: zlib-cli doctor` |
| Rate limit or quota | `upstream_rate_limited` | true / not_applied | 3 | `Check: zlib-cli quota` |
| Timeout | `upstream_timeout` | true / not_applied | 3 | |
| Other network failure | `upstream_network_error` | true / not_applied | 3 | `Run: zlib-cli doctor` |
| Unexpected upstream response | `upstream_protocol_changed` | false / not_applied | 1 | |
| The service refused the call | `upstream_refused` | false / not_applied | 1 | |
| Another upstream failure | `upstream_error` | false / not_applied | 1 | |
| The saved session is locked by another process | `session_busy` | true / not_applied | 3 | `Try again in a moment.` |
| Another process changed the saved session | `session_conflict` | false / not_applied | 4 | `Run: zlib-cli status` |
| Another saved-session problem | `session_error` | false / not_applied | 1 | |
| Local I/O failure, including a config file that cannot be read or written | `local_io_error` | false / not_applied | 1 | |
| Textual is not installed (`tui`) | `missing_dependency` | false / not_applied | 1 | `python -m pip install ".[tui]"` |
| Ctrl+C, end of input, declined confirmation | `cancelled` | false / not_applied | 130 | |
| Unexpected error | `internal_error` | false / not_applied | 1 | `Set ZLIB_DEBUG=1 to show details.` |

### Account changes

The nine changes are `favorites add` and `remove`, `tags add` and `remove`, and `booklists add`,
`remove`, `create`, `edit` and `delete`. As in the HTTP API, a change whose result is in doubt never
claims `not_applied`:

| Condition | `code` | retryable / outcome | Exit |
|---|---|---|---|
| A network, protocol or other failure during the change | `mutation_uncertain` | false / **unknown** | 4 |
| Rate limit | `upstream_rate_limited` | false / not_applied | 3 |
| Ctrl+C during the change | `cancelled` | false / **unknown** | 130 |
| Sign-in, verification, refusal, booklist not found | as for reads | | |

When the outcome is `unknown`, the hint names the read that shows the current state:
`Check with: zlib-cli favorites`, `Check with: zlib-cli tags saved-ids`,
`Check with: zlib-cli booklists show BOOKLIST_ID` (add and remove) or `Check with: zlib-cli booklists`
(create, edit and delete). Nothing retries automatically.

A list change (`favorites add -`, `tags add -a FILE TAG`, ...) reports every item instead:
`{"items": [{"position", "reference", "status", "code"}], "summary": {"applied", "failed", "uncertain",
"not_started"}}` on stdout, with exit 0 (all applied), 4 (any failed or uncertain), 3 (a rate limit stopped
the run) or 130. `--dry-run` prints `{"dry_run": true, "action", "domain", ...}` and exits 0. Details:
"Account changes in the CLI" in `docs/reference.md`.

`queue add` works the same way on the local API: `state` is the task's state when the server accepted the
item, else `failed`, `uncertain` (the request may have reached the server; `code` is
`local_api_unavailable` or `cancelled`) or `not_started`. A busy or rate-limited server stops the run (exit 3
if nothing else went wrong), as does a lost connection, a rejected token or disabled downloads. Each item is
sent once; running the same command again with the same `--key` replays the tasks already created.

### Single-book download

A download uses quota once its link request is sent. Errors of `download` with one book carry
`quota_may_be_used`:

| Condition | `code` | retryable / outcome | `quota_may_be_used` | Exit |
|---|---|---|---|---|
| No download link offered (`unavailable`, `no_link`), explicitly refused | `download_unavailable` | true / not_applied | false | 3 |
| The service refused the download | `upstream_refused` | false / not_applied, or unknown | false or true | 1, or 4 |
| Another download failure (`invalid_book`, `format_mismatch`, `protocol`, `size_limit`, `local_write`, `save_failed`, `download`) | `download_failed` | false / not_applied, or unknown | false or true | 1, or 4 |
| Any other error after the link request | the cause's code (for example `upstream_timeout`) | false / **unknown** | true | 4 |
| Cancelled transfer or Ctrl+C | `cancelled` | false / not_applied, or unknown | false or true | 130 |
| Any other error before the link request, or an explicit refusal of it | as for reads | | false | as for reads |

When `quota_may_be_used` is true, the hint is `The download may have used quota. Check zlib-cli quota
and zlib-cli history before downloading this book again.` Download that book again only after checking.

### Local API (`jobs`, `tui`)

`jobs` passes through the error code of the running server (`zlib-cli serve`), for example
`download_not_found`, `download_conflict`, `download_not_ready`, `downloads_busy`,
`downloads_unavailable`, `unauthorized` or `upstream_rate_limited`, with its `retryable` and
`outcome`. The server's message is never shown. A response without a valid error object gives
`local_api_error` (exit 1). Hints: `unauthorized`: `Use the token file of the running server
(--token-file).`; `downloads_unavailable`: `Start the server without --no-downloads or --anonymous.`;
`download_conflict`: `Use a new --key for a different book.`

When the server cannot be reached, the code is `local_api_unavailable`:

| Action | retryable / outcome | Exit | Hint |
|---|---|---|---|
| `list`, `show`, `watch`, `file`, `tui` | false / not_applied | 1 | `Start the server: zlib-cli serve` |
| `add` | false / **unknown** | 4 | `Run the same command again with the same --key: the server replays it and never adds a second download.` |
| `cancel` | false / **unknown** | 4 | `Check with: zlib-cli jobs show TASK_ID` |
| `resume` | false / **unknown** | 4 | `Check with: zlib-cli jobs list` |

`jobs watch` exits 0 when the task `succeeded`, 4 when it ended `failed`, `interrupted` or
`cancelled`, and 3 when `--duration` ended first. In text mode, a last stderr line says
`The task ended in state failed.` or
`Stopped watching before the task finished. Run the same command to keep watching.`

## Prompts and input

The CLI asks a question (an email, a confirmation, a search result choice, the `shell` prompt) only
when one of these allows it, checked in this order:

1. `--interactive` is given: allowed, and answers are read from stdin.
2. `--no-input` is given, or `ZLIB_NO_INPUT` is set to a non-empty value other than `0`: not allowed.
3. Otherwise: allowed only when stdin and stderr are both terminals. On Windows a terminal is a
   console, so a `NUL` redirect does not count as one.

A password is read only from a terminal (`getpass`), even with `--interactive`; it is never read from
a pipe. `--json` does not change these rules.

When a question would be needed but is not allowed, the command fails before any network request:

| Where | Message | `error` / `code` | Exit |
|---|---|---|---|
| An account change without `--yes` | `Use --yes to apply an account update in non-interactive mode.` | `ValueError` / `input_required` | 2 |
| `download QUERY` without `--pick` | `Choosing a search result needs a terminal. Add --pick N, or download an ID:HASH reference.` | `InputRequired` / `input_required` | 2 |
| `login`, `auth-test` without the email and password | `Signing in needs a terminal to ask for the email and password, or both in the credentials file.` | `InputRequired` / `input_required` | 2 |
| Another account command (including `serve` and list downloads) without a saved session | `No saved session, and no terminal to sign in.` Hint: `Run zlib-cli login in a terminal first.` | `AuthenticationError` / `upstream_authentication_required` | 5 |
| `shell` | `The shell needs a terminal. Use search and download in scripts, or add --interactive to read commands from standard input.` | `InputRequired` / `input_required` | 2 |
| A list download without `--yes` or `--pick` | The list messages (`Use --yes ...`, `N items need a search result choice ...`) | `ValueError` / `input_required` | 2 |
| `tui` without a terminal | `The TUI needs an interactive terminal. ...` | `ValueError` / `input_required` | 2 |

`serve` checks this before it binds its port. A confirmation accepts `y` or `yes`; any other answer,
or end of input, cancels (exit 130). The search result choice is `Choose 1-N, or q to cancel:`; it
asks again after an answer that is not a number from 1 to N, and `q`, end of input and Ctrl+C cancel.
On `download`, `--interactive` also selects the list flow, so a piped title runs as a one-book list
that asks for the choice and then a confirmation.

## Settings

A settings error names the key and its source, never the value:

```json
{"error": "SettingsError", "message": "The config file has an invalid value for timeout (C:\\Users\\me\\AppData\\Roaming\\zlibrary-api\\config.toml).", "code": "config_invalid", "exit_code": 2, "retryable": false, "outcome": "not_applied", "key": "timeout", "source": "config", "hint": "Run: zlib-cli config unset timeout"}
```

A bad value given on the command line (`--timeout 0`, `config set timeout abc`) stays
`invalid_argument` (exit 2), with `source: "cli"`. `zlib-cli mcp` reports a settings problem as its
usual one-line startup refusal (exit 1).

`config` shapes (`--json`, fake values):

| Command | stdout |
|---|---|
| `config path` | `{"path": "...config.toml", "exists": false, "source": "default", "credentials_path": "...credentials.toml", "credentials_exists": false}`. `source` is `cli` (`--config`), `env` (`ZLIB_CONFIG`) or `default`. `credentials_path` is the file a sign-in would read, else the default place for one |
| `config list` | `{"path": ..., "exists": true, "settings": {"domain": {"value": null, "origin": "default"}, "timeout": {"value": 60, "origin": "config"}, "languages": {"value": ["english"], "origin": "env", "variable": "ZLIB_LANGUAGES"}, ...}}`, with all 7 keys. `origin` is `cli` (plus `option`), `env` (plus `variable`), `config` or `default`. An unset list is `[]` |
| `config get KEY` | `{"key": "formats", "value": ["epub"], "origin": "config"}`, plus `option` or `variable` |
| `config set KEY VALUE...` | `{"key": "formats", "value": ["epub", "pdf"], "previous": null, "path": ...}` |
| `config unset KEY` | `{"key": "formats", "removed": true, "previous": ["epub"], "path": ...}` |

## Success shapes (`--json`, fake values)

| Command | stdout |
|---|---|
| `search` | An array of Book (`title`, `authors`, `extension`, `url`, `year`, `language`, `size`, `book_id`, `book_hash`, `is_favorite`). With `--with-pagination`: `{"books": [...], "pagination": {page, limit, total_items, total_pages, next_page, previous_page}}` |
| `info` | BookDetails: Book plus `description`, `cover_url`, `identifier`, `isbn10`, `isbn13`, `publisher`, `edition`, `pages`, `categories` |
| `formats` | An array of `{book_id, book_hash, extension, size, download_supported}` |
| `download` (one book) | `{"path": "..."}` |
| `download` (list) | A batch plan or report document (indented), see the [technical reference](reference.md#batch-downloads) |
| `profile` / `quota` | `{"quota": {limit, used, remaining}}` / `{limit, used, remaining}` |
| `similar`, `popular`, `recommended` | An array of Book |
| `favorites`, `history`, `booklists show` | `{books, pagination}`. Booklist books add `booklist_id` and `entry_id` |
| `favorites add`, `favorites remove` | `{book_id, is_favorite}` |
| `tags` (list) / `tags saved-ids` / `tags add`, `tags remove` | `{"tags": [...]}` / `{"book_ids": [...]}` / `{book_id, action, tags, adds_to_favorites}` |
| `booklists` (list) | An array of `{booklist_id, title, description, book_count, is_public, updated_at, color}` |
| `booklists add`, `remove` / `create`, `edit` / `delete` | `{booklist_id, action, book_id, entry_id}` / a booklist object / `{booklist_id, deleted: true}` |
| `library` | `{favorites: {books, pagination}, history: {books, pagination}, booklists: [...]}` |
| `login` | `{"authenticated": true, "stored": true, "domain": ...}` |
| `logout`, `status` | `{state, domain, saved_at, expires_at, verified}`. `status --verify` adds `authenticated: true` and sets `verified: true` |
| `doctor` | `{results: [{domain, search_ok, checked_at, elapsed_ms, final_domain, result_count, error_code, message}], working_domains, recommended_domain, proxy}` (`proxy` is the `http://HOST:PORT` every probe went through, or null) |
| `domains` | An array of strings |
| `check` / `auth-test` (always JSON) | `{reachable, domain, api, search_ok, results, authenticated}` / `{"authenticated": true}` |
| `jobs add`, `show`, `cancel` / `list` / `resume` / `file` / `watch` | A task `{id, reference, state, created_at, updated_at, progress, cancel_requested, error_code, file_available, error_message, outcome}` / `{tasks, total, limit, offset, paused}` / the server's resume object (`resumed` count) / `{"path": ...}` / NDJSON events `{id, task, paused}` |
| `queue add` / `list` / `resume` | `{items: [{position, reference, key, task_id, state, code}], summary: {submitted, failed, uncertain, not_started}}` / as `jobs` |
| `config path`, `list`, `get`, `set`, `unset` | See [Settings](#settings) |
| `serve --json` | One NDJSON line once it listens: `{"event": "listening", "url": "http://127.0.0.1:PORT/v1", "token_file": ..., "downloads": false}`. The token is never printed |
| `completion` | The shell script as text, even with `--json` |
| `serve` (text), `shell`, `tui`, `mcp` | No data on stdout (`mcp` speaks JSON-RPC there) |

## Migration from earlier versions

| Change | Who is affected | What to do |
|---|---|---|
| Single commands use exit codes 2, 3, 4 and 5: a local validation error is 2 (was 1), a sign-in problem 5 (was 1), a rate limit or network error on a read 3 (was 1), an uncertain account change or download 4 (was 1) | Scripts that test `== 1` | Test `!= 0`, or use the table above. Scripts that test `!= 0` are unaffected |
| `jobs watch` exits 3 or 4 instead of 0 when the task did not succeed | Scripts that chain `jobs watch && ...` | 0 now means `succeeded`. Read the last event, or test the code |
| No prompt reads piped stdin unless `--interactive` is given | `echo 1 \| zlib-cli download dune`, text piped into `shell` | Use `--pick 1`. Add `--interactive` to keep piped answers. Piped passwords were never supported |
| An account change without a terminal is `input_required`, exit 2 (was `ValueError`, exit 1) | Scripts that test `== 1` | Add `--yes` |
| JSON is ASCII-escaped everywhere | Tools that read raw bytes | Parse the JSON; the values are identical |
| New error keys | None (keys are only added) | Use `code` instead of `error` |
| JSON errors where plain text was printed before: declined confirmation, end of input, Ctrl+C, `OSError`, usage errors, unexpected exceptions | Tools that read stderr text in JSON mode | Parse one JSON object |
| No traceback by default | Developers | Set `ZLIB_DEBUG=1` |
| Global options and `--json` are accepted in any position | None (only new forms) | |
| `download --interactive` | None | It still selects the list flow and is now also accepted before the command |
| Settings: `config.toml` and the `ZLIB_DOMAIN`, `ZLIB_TIMEOUT`, `ZLIB_DOWNLOAD_DIR`, `ZLIB_LANGUAGES`, `ZLIB_FORMATS`, `ZLIB_API_URL`, `ZLIB_CREDENTIALS` and `ZLIB_CONFIG` variables now take effect | Shells that already define one of these names for something else; machines with a config file | Unset the variable, or pass `--no-config` and explicit options. Without a file and these variables nothing changes |
| A named credentials file without `domain` no longer forces `https://z-lib.by/` over the saved session's domain | Scripts that relied on that mismatch error | None: the session's domain is used |
| Credentials are looked up in the settings folder first, then in the legacy `credentials.local.toml`, which prints a one-line stderr note | Tools that expect an empty stderr | Move the file to `credentials.toml` in the settings folder (`zlib-cli config path --json` shows `credentials_path`) |
| `serve` listens on a configured `api_url` | Only when `api_url` is set in the environment or the file | Pass `--host` and `--port` to override |
