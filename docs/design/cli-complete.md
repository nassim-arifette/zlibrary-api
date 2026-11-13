# zlib-cli: complete CLI implementation spec

Design, 2026-11-11. Status on 2026-11-13: D1 to D3 implemented (see `docs/cli-json.md` and the Settings section of
`docs/reference.md`) and D4 (see "Account changes in the CLI" in `docs/reference.md`); D5 to D7 not
implemented yet. The owner decided the open questions on 2026-11-12 (section 10).

- **Request:** the owner's "cli complet" item: a coherent command set, JSON for every command (errors
  included), documented exit codes, a strict non-interactive mode, lists on standard input, messages kept
  apart from data, readable help and tables, shell completion, persistent preferences, explicit account
  changes, and compatibility or documented migration.
- **Baseline:** `main` at `29ee46b` (batch downloads merged), package version `0.1.0`, Python >= 3.11,
  stdlib-only core (`[tui]` adds Textual). Paths are relative to the repository root. Line numbers refer to
  that commit.
- **Companion:** the MCP server (`zlib-cli mcp`) has its own design document. Section 9 lists what the CLI
  and MCP share.
- **What is specified:** this document splits the work into seven deliverables (section 3). D1 and D2 are
  specified in full (sections 4 and 5). D3 to D7 have a scope and acceptance criteria (section 6).

## 1. Rules for every deliverable

1. **Never choose an edition silently.** A search result is chosen through a prompt, with `--pick N`, or
   from an exact `ID:HASH` reference. Preferences filter results; they never choose one.
2. **Never retry an uncertain operation automatically.** This covers a download after its quota-using link
   request and an account change that may have been applied. Report it with `outcome: "unknown"` and exit
   code 4. Hints may tell a person what to check, but nothing acts on them automatically.
3. **Data goes to stdout.** Prompts, candidate lists, notes, progress and errors go to stderr.
4. **Literal, terminal-safe text.** Remote text passes through `terminal_text()`. Text that the CLI writes
   itself is ASCII: D1 and D2 add ASCII text only, and D3 enforces this with a test.
5. **No secrets in output.** No password, email, cookie, API token or upstream response body appears in
   output, errors or notes. The config file never stores secrets.
6. **Explicit account changes.** In a terminal, an account change asks `[y/N]`. Otherwise it needs `--yes`.
   `--yes` confirms only the change it accompanies and never picks an edition.
7. **Offline synthetic tests.** No test contacts Z-Library or a mirror. CI runs
   `python -m unittest discover -s tests -v` on Windows and Linux with Python 3.11 and 3.14.
8. **One feature per commit.** Each deliverable is one short English commit that contains its code, tests
   and docs, merged through a PR with CI green.
9. **Backward compatibility.** Every incompatible change has a migration note in the docs and in the PR
   description.

## 2. Facts this spec relies on

### 2.1 Code entry points (`29ee46b`)

| What | Where |
|---|---|
| Console script | `pyproject.toml`: `zlib-cli = "zlib_connector.cli:main"`. `python -m zlib_connector` runs `zlib_connector/__main__.py` |
| Dispatch and error handling | `zlib_connector/cli.py:89-254` (`main`). Line prompt `cli.py:28-33`, `shell` `cli.py:42-87`, account-change TTY check `cli.py:145-146`, confirmation `cli.py:190-193`, sign-in `cli.py:194-195`, result prompt `cli.py:227-231`, download JSON `cli.py:237`, exception handlers `cli.py:239-254` |
| Parser | `zlib_connector/cli_parser.py:21-142`. Global options on the root only (`:23-25`). One `--json` per command (`:45`, `:58`, `:60`, ...). Action-level `--json` uses `default=argparse.SUPPRESS` (`:81`) |
| Session options, `AccountSession` | `zlib_connector/cli_session.py:9-20` (`--session-file`, `--no-session`, login/logout/status parsers), `:23-72` (domain chain `:47-54`, email and password prompts `:67-68`), `:75-93` (`local_session_command`) |
| Confirmation text, mutation calls | `zlib_connector/cli_views.py:15-37` (`account_confirmation`). Mutating Client calls: `:126` (tags), `:152` (favorites), `:162`, `:165` (booklist add, remove), `:173`, `:175`, `:177` (booklist create, edit, delete) |
| Batch CLI | `zlib_connector/cli_batch.py`: exit-code docstring `:1-8`, safe writer `_write` `:95-102`, terminal test `_tty` and `_console` `:109-133`, `_interactive` `:369-372`, strict checks `:375-419`, choice prompt `:500-517`, confirmation `:544-569`, plan and report JSON with `indent=2` `:604`, `:693` |
| `serve` | `zlib_connector/http_cli.py`: parser `:17-30`, prompts `:33-42`, listener checks `:45-70`, bind then sign in `:73-95` |
| `jobs` | `zlib_connector/cli_jobs.py`: parser `:16-37`, `run_jobs` `:40-81`, `_watch` `:84-121` |
| `tui` launcher | `zlib_connector/cli_tui.py:9-34` (terminal check `:21-22`, missing Textual `:27`, token `:34`) |
| Progress | `zlib_connector/cli_progress.py:9-13` (options), `:23-29` (automatic in a terminal) |
| Error types | `zlib_connector/errors.py`: `ConnectorError` family, `NetworkError.reason`, `DownloadError.reason`. `BooklistNotFoundError` subclasses `ValueError` |
| HTTP error envelope | `zlib_connector/http_api/contract.py:13-25` (`RequestError.to_dict()`). Mapping in `zlib_connector/http_api/server.py:76-102` (`_upstream_error`). Task route codes in `zlib_connector/http_api/downloads.py:27-38`, `:126`, `:153` |
| Local API client | `zlib_connector/local_api.py:27-44` (`LocalAPIError` keeps only the HTTP status), `:80-82` (`_status_error`), `:228-233` (`_network_error`) |
| Download stage | `zlib_connector/eapi.py:359-394`. After the link request, an exception carries `download_stage` = `"link"` or `"transfer"`. Quota classifier: `zlib_connector/batch.py:663-669` (`_may_have_used_quota`) |
| Credentials | `zlib_connector/credentials.py:6` (`DEFAULT_FILE` = `credentials.local.toml` beside the package), `:14-27` (`load_credentials`; allowed keys `domain`, `email`, `password`) |
| State directory | `zlib_connector/local_state.py:7-17`: `ZLIB_STATE_DIR`, else `%LOCALAPPDATA%\zlibrary-api`, else `${XDG_STATE_HOME:-~/.local/state}/zlibrary-api` |
| Protected writes | `zlib_connector/sessions.py:161-250` (lock, generation compare, private temporary file, fsync, `os.replace`, bounded Windows retry for errors 5 and 32 at `:201-231`). `session_lock.private_lock` (`:79`). `private_files.create_private_file` (`:260`) |
| Validators | `session_data.canonical_origin` (`:14`), `search._choices` (`:21-39`), `client.SUPPORTED_FORMATS` (`:14`), `local_api._address` (`:46-62`), `jobs.models.validate_idempotency_key`, `terminal_text()` (`terminal_text.py:10`) |

### 2.2 Behavior today (checked with offline probes on `29ee46b`)

- **Commands (25):** `login`, `logout`, `status`, `jobs`, `tui`, `search`, `download`, `info`, `formats`,
  `profile`, `quota`, `similar`, `popular`, `recommended`, `favorites`, `history`, `tags`, `library`,
  `booklists`, `auth-test`, `shell`, `check`, `doctor`, `domains`, `serve`.
- **Exit codes:** 0 for success. 1 for any `ConnectorError`, `ValueError` or `OSError`. 2 for an argparse
  usage error. 130 for a cancellation. Batch adds 3 and 4. `doctor` exits 1 when no domain works.
  `jobs watch` always exits 0.
- **JSON errors** go to stderr as `{"error": TypeName, "message": ...}`, with no code; the
  `NetworkError` and `DownloadError` reasons are dropped. Even with `--json`, these cases print plain
  text: a declined confirmation, EOF, Ctrl+C, `OSError` and argparse errors. Any other exception prints a
  traceback.
- **Encoding:** most JSON output uses `ensure_ascii=False`. On Windows, redirected stdout uses the ANSI code
  page (cp1252), so one non-ASCII title makes `search` and `search --json` fail with
  `UnicodeEncodeError` (exit 1, empty stdout).
- **Prompts:**
  - `Result number:` reads stdin without checking for a terminal. Typing `abc` gives an `int()` error.
  - The email prompt reads stdin.
  - The password prompt uses `getpass`. On Windows it reads the console keyboard, not redirected stdin.
  - Account confirmations test `sys.stdin.isatty()`, which is true for `< NUL` on Windows.
- **Option position:** global options are accepted only before the command (`zlib-cli domains --timeout 5`
  exits 2). `--json` is accepted only after it.
- **Version and settings:** there is no `--version` and no config file. The only environment variables
  read are `ZLIB_STATE_DIR`, `LOCALAPPDATA`, `XDG_STATE_HOME` and `NO_COLOR` (TUI).
- **Domain chain:** `--domain`, then the domain of an explicit `--credentials` file, then the saved
  session's origin, then the default credentials file's domain, then `https://z-lib.by/`.
  - A domain that differs from a restored session's origin is refused (`cli_session.py:53-54`).
  - `Credentials.domain` defaults to `https://z-lib.by/` when the file has no `domain`.

### 2.3 argparse facts (probed on Python 3.11 and 3.14)

- An option defined in a parent parser with `default=argparse.SUPPRESS` and added to the root and to every
  subparser works before or after the command, and after an action (`favorites add X --json`). The last
  occurrence wins.
- argparse checks mutual exclusion only within one parser level. `--no-input domains --interactive` is
  accepted, so the conflict needs a check after parsing.
- Overriding `ArgumentParser.error()` intercepts every usage error, including a missing command.
- A subparser's own defaults overwrite values set at the root. This is why every shared option must use
  `SUPPRESS` and get its default after parsing.
- Prefix abbreviations are allowed (`allow_abbrev=True`). This spec does not change that.

### 2.4 Platform and format facts

- **Windows:**
  - `isatty()` is true for `NUL`. A real console is a stream for which `GetConsoleMode` succeeds; the
    `_console` helper already checks this.
  - Redirected stdio uses the ANSI code page unless `PYTHONUTF8=1` or `PYTHONIOENCODING` is set.
- **Config locations by convention:**
  - Windows keeps roaming settings in `%APPDATA%`.
  - POSIX follows the XDG Base Directory spec: `$XDG_CONFIG_HOME`, default `~/.config`. Relative values
    must be ignored.
- **TOML:** `tomllib` (Python 3.11+) reads TOML 1.0. The standard library has no TOML writer.
- **Version:** `importlib.metadata.version("zlib-connector")` returns the installed version. In a checkout
  that is not installed, it raises `PackageNotFoundError`.

## 3. Deliverables, ordered by user value

| # | Deliverable | User value | Commit message |
|---|---|---|---|
| D1 | **Scriptable contract:** error codes and exit codes, safe output encoding, no prompt without input, global options in any position, `--version` | Scripts and agents can rely on the CLI. No crash on Windows pipes. No hidden prompt that hangs | `Add scriptable CLI errors and prompts` |
| D2 | **Settings:** `config.toml`, `ZLIB_*` variables, the `config` command, credentials lookup, `serve --downloads` | The domain, timeout, folder, languages and formats persist. One resolver serves the CLI, `serve`, the TUI and MCP | `Add CLI settings and config command` |
| D3 | **Help, examples and tables** | Readable one-line book rows and discoverable commands | `Add CLI help examples and tables` |
| D4 | **Clear account changes:** described prompts, `--dry-run`, lists of references | Safer deletions and bulk account changes | `Add clearer account changes and dry runs` |
| D5 | **`queue`, `recent` and `serve --json`** | Queue work without knowing `jobs` or HTTP | `Add queue and recent commands` |
| D6 | **Shell completion** | Faster typing | `Add shell completion` |
| D7 | **HTTPS proxy** (HTTP CONNECT) | Access from restricted networks | `Add HTTPS proxy support` |

Dependencies:

- D2 reports its errors through D1.
- D3 extends D1's parser.
- D4 uses D1's confirmation helpers and D3's table renderer.
- D5 uses D2's `api_url` and `download_dir`.
- D6 uses D3's command table.
- D7 uses D2's settings and needs its own security review.
- The MCP server depends on D1 (`error_codes`) and D2 (`settings`).

---

## 4. D1: scriptable contract (full spec)

### 4.1 Scope

**In scope:**

- shared error codes and a documented exit-code table;
- one JSON error object for every failure;
- safe stream encoding and ASCII JSON;
- one terminal test and an input policy (`--no-input`, `ZLIB_NO_INPUT`, `--interactive`) applied to every
  prompt;
- a choice prompt that can no longer crash;
- global options accepted before or after the command;
- `--version`;
- `jobs watch` exit codes;
- pass-through of the local API's error codes;
- `docs/cli-json.md`.

**Out of scope (later deliverables):**

- grouped help, examples and tables (D3);
- `--quiet` and global `--progress` (D3);
- prompt wording for account changes (D4);
- `serve --json` readiness (D5).

### 4.2 Modules

| Module | Change |
|---|---|
| `zlib_connector/error_codes.py` (new, shared, no CLI or HTTP imports) | `ErrorCode` (a NamedTuple of `code`, `retryable`, `outcome`, `quota_may_be_used`). `classify(error, *, mutation=False, download=False)` (section 4.7). `may_have_used_quota(error, stage)`, moved unchanged from `batch.py:663`; `batch.py` imports it |
| `zlib_connector/http_api/server.py` | `_upstream_error()` calls `classify(error, mutation=...)` and maps the code to `(status, fixed message)` through a table. HTTP behavior must not change: the existing HTTP tests pass unmodified. `ValueError` handling stays as it is |
| `zlib_connector/cli_output.py` (new) | `prepare_streams()`, `print_json()`, `report_error()`, `exit_code_for()`. Exception types: `UsageError(ValueError)`; `CommandError(ValueError)` with `code`, `hint` and `name`, the value reported as `error` (default `"ValueError"`, so re-coded `ValueError`s keep their JSON `error`); `InputRequired(CommandError)` (`name="InputRequired"` for the new prompt guards); `Cancelled(Exception)` |
| `zlib_connector/cli_terminal.py` (new) | `is_terminal(stream)`, moved from `cli_batch._tty` and `_console` without changing the logic. `InputPolicy`. The prompt helpers `ask(label)`, `confirm(question)`, `choose(count)` and `password(label)` |
| `zlib_connector/cli_parser.py` | `CLIParser` (its `error()` raises `UsageError`). `common_options()` parent parser. `GLOBAL_DEFAULTS`. `finish(args)` fills defaults and checks conflicts. The per-command `--json` flags and `download --interactive` are removed; the shared options replace them |
| `zlib_connector/cli.py` | `main()` calls `prepare_streams()`, parses, calls `finish()`, builds the `InputPolicy` and dispatches. All failures go to `report_error()`. The result choice uses `choose()`. A local `operation` variable records whether the failing call was a read, an account change or a download (section 4.7). `cli.prompt` and `cli.getpass` stay as test seams |
| `zlib_connector/cli_session.py` | `AccountSession.needs_prompt`: true when no session will be restored and the email or password is missing |
| `zlib_connector/cli_batch.py` | Uses `is_terminal` and the `InputPolicy`. The existing "needs `--yes`" and "needs `--pick`" `ValueError`s become `InputRequired`, with the same messages |
| `zlib_connector/http_cli.py` | `run_server()` checks the input policy before `server_factory(...)` binds |
| `zlib_connector/cli_jobs.py` | `run_jobs()` returns an exit status (watch: 0, 3 or 4). JSON goes through `print_json` |
| `zlib_connector/local_api.py` | `LocalAPIError(status, code=None, retryable=None, outcome=None)` parses the server's error body (section 4.7). Its `message` is unchanged |
| `zlib_connector/cli_tui.py` | Uses `is_terminal`. "Needs an interactive terminal" becomes `InputRequired`. Missing Textual becomes `CommandError(code="missing_dependency")` |

### 4.3 Global options

Every option below is defined once in `common_options()` with `default=argparse.SUPPRESS`. It is added to
the root parser and to every command and action parser. `finish(args)` then sets the missing attributes
from `GLOBAL_DEFAULTS`.

| Option | Default | Meaning |
|---|---|---|
| `--domain URL` | none | As today |
| `--credentials FILE` | none | As today |
| `--timeout SECONDS` | 30 | As today: more than 0, at most 300 |
| `--session-file FILE` or `--no-session` | none / off | As today; the two exclude each other |
| `--json` | off | JSON on stdout. Errors become one JSON object on stderr |
| `--no-input` | off | Never prompt (section 4.4). The environment equivalent is `ZLIB_NO_INPUT=1` |
| `--interactive` | off | Allow prompts without a terminal; answers are read from stdin. On `download`, it still selects the list flow, as today: `BATCH_OPTIONS` keeps `interactive`, so `is_batch()` is unchanged |
| `--version` | | Root only. Prints `zlib-cli X.Y.Z` (or `zlib-cli unknown`) on stdout and exits 0 |

- Every option above is accepted before the command, after it and after an action. The last occurrence
  wins.
- `finish()` rejects conflicts at any position: `--no-input` with `--interactive`, and `--session-file` with
  `--no-session`. The message uses argparse's wording (`argument --interactive: not allowed with argument
  --no-input`), with code `invalid_argument` and exit 2.
- In `shell`, a usage error inside a `search` line still prints text and the loop continues: `UsageError`
  is a `ValueError`, which the loop already catches. The loop keeps catching `SystemExit`, which `--help`
  raises.

### 4.4 Input policy and prompts

**Line prompts** (email, confirmation, result choice, `shell`) are allowed when:

1. `--interactive` is given: allowed.
2. `--no-input` is given: not allowed.
3. `ZLIB_NO_INPUT` is set to a non-empty value other than `0`: not allowed.
4. Otherwise: allowed only if stdin and stderr are both terminals (`is_terminal`, which includes the Windows
   console-mode check).

**The password prompt** (`getpass`) also needs stdin to be a terminal, even with `--interactive`. A
password is never read from a pipe.

**`--json`** does not change the policy: people use `--json` in a terminal. Agents and the MCP server
always behave as `--no-input`.

**When input would be needed but is not allowed, the command fails before any network request:**

| Prompt site | Condition | Message (ASCII) | `error` / `code` | Exit |
|---|---|---|---|---|
| Account confirmation (the 9 mutating actions) | No `--yes` | Unchanged: `Use --yes to apply an account update in non-interactive mode.` | `ValueError` / `input_required` | 2 (was 1) |
| Result choice (`download QUERY`) | No `--pick`, not a reference | `Choosing a search result needs a terminal. Add --pick N, or download an ID:HASH reference.` | `InputRequired` / `input_required` | 2 |
| Email or password for `login` and `auth-test` | `needs_prompt` | `Signing in needs a terminal to ask for the email and password, or both in the credentials file.` | `InputRequired` / `input_required` | 2 |
| Email or password for any other account command, including `serve` and list downloads | `needs_prompt` | `No saved session, and no terminal to sign in.` Hint: `Run zlib-cli login in a terminal first.` | `AuthenticationError` / `upstream_authentication_required` | 5 |
| `shell` | Always | `The shell needs a terminal. Use search and download in scripts, or add --interactive to read commands from standard input.` | `InputRequired` / `input_required` | 2 |
| `serve` sign-in | `needs_prompt` | Same as the other account commands. **The check runs before the port is bound** | as above | 5 |
| Batch | Existing rules (`cli_batch.py:381`, `:416`) | Unchanged messages | `ValueError` / `input_required` | 2 (was 1) |
| `tui` | stdin or stdout is not a terminal | Unchanged message | `ValueError` / `input_required` | 2 (was 1) |

For the batch flow, `_interactive(args)` becomes `policy.allowed and args.batch_file != "-"`.

**Prompts that remain:**

- **Account confirmation.** The question text does not change in D1 (D4 rewrites it). `y` or `yes`
  (case-insensitive) applies the change. Any other answer raises `Cancelled("Cancelled. Nothing was
  changed.")`: exit 130, and a JSON object in JSON mode.
- **Result choice.** The candidates are listed on stderr as today. The prompt is
  `Choose 1-N, or q to cancel: `.
  - An answer that is not a number from 1 to N prints `Type a number from 1 to N, or q.` and asks again.
  - `q`, EOF and Ctrl+C mean cancelled (130).
  - The `int()` crash is gone.
- **Email and password.** The text does not change.

### 4.5 Output rules

1. **Streams.** `prepare_streams()` runs first. It calls `reconfigure(errors="backslashreplace")` on stdout
   and stderr when the stream has `reconfigure`. Text output then never crashes on a legacy code page;
   characters the code page cannot encode appear as backslash escapes. Docs recommend `PYTHONUTF8=1` for
   UTF-8 pipelines.
2. **JSON.** `print_json(value, indent=None)` writes `json.dumps(value, ensure_ascii=True,
   allow_nan=False)` with the default separators, then `\n`, then flushes.
   - Batch plans and reports keep `indent=2`.
   - `jobs watch --json` writes one event per line (NDJSON).
   - A serialization failure is reported as `internal_error`, never as `invalid_argument`.
3. **Success shapes do not change** (section 4.10 lists them). New keys may be added later. JSON consumers
   must ignore unknown keys, the same rule as the HTTP `/v1` contract.
4. **Errors.**
   - In JSON mode, a failure writes exactly one JSON object line on stderr and nothing on stdout.
   - In text mode, the first line stays `TypeName: message` (unchanged). An optional second line gives
     `Hint: ...`.
   - "JSON mode" means that the exact token `--json` appears anywhere in argv before a `--` separator. This
     lets argparse usage errors be JSON too. Abbreviations such as `--js` are not detected.
5. **Partial success with data** prints the data on stdout and exits non-zero without an error object:
   batch (3 or 4), `doctor` (1) and `jobs watch` (3 or 4).
6. **Unexpected exceptions** become `internal_error`, exit 1, with the message
   `Unexpected error. Set ZLIB_DEBUG=1 to show details.`. With `ZLIB_DEBUG=1`, the traceback follows on
   stderr. No other path prints a traceback.

### 4.6 Error object

```json
{"error": "NetworkError", "message": "The request timed out.", "code": "upstream_timeout", "exit_code": 3, "retryable": true, "outcome": "not_applied", "reason": "timeout", "hint": "Run: zlib-cli doctor"}
```

| Key | Type | Present | Meaning |
|---|---|---|---|
| `error` | string | always | **Unchanged** wherever a JSON error was printed before: the exception type name, or a `CommandError`'s `name` (`ValueError` where a `ValueError` was raised before). New values appear only where plain text or a traceback was printed before: `UsageError`, `Cancelled` (declined, EOF, Ctrl+C), `InputRequired` (new prompt guards), `InternalError`, and `OSError` (was plain text). `TransferCancelled` is kept. Scripts should switch to `code` |
| `message` | string | always | `str(error)` for `ConnectorError` and `ValueError` (unchanged). A fixed text otherwise, for example the `OSError` text `Local I/O failed. Check the destination and permissions.` |
| `code` | string | always | A stable snake_case code (section 4.7) |
| `exit_code` | integer | always | The process exit status |
| `retryable` | boolean | always | Same meaning as in the HTTP API: the same request may succeed later without changes. It is advisory only; nothing retries on its own |
| `outcome` | `"not_applied"` or `"unknown"` | always | `unknown` when an account change or a quota-using download may have taken effect |
| `reason` | string | when the exception has one | `NetworkError.reason` (`dns`, `blocked_address`, `certificate`, `tls`, `timeout`, `redirect`, `http`, `network`) or `DownloadError.reason` |
| `quota_may_be_used` | boolean | errors of a single-book `download` raised by `client.download` | `true` once the link request was sent, by the same rule batch uses |
| `hint` | string | optional | A literal next step in ASCII. It may include an ID that was already validated, and never a secret |

The text form of the same error:

```text
NetworkError: The request timed out.
Hint: Run: zlib-cli doctor
```

### 4.7 Error codes and exit codes

**Exit codes** (printed in the root `--help` epilog and in `docs/cli-json.md`):

| Code | Meaning |
|---|---|
| 0 | Success, including empty results. `doctor` found a working domain. `jobs watch` saw `succeeded` |
| 1 | Failed: a refusal, a browser challenge, a protocol change, not found, a local I/O or local API problem, an internal error. `doctor` found no working domain |
| 2 | Usage: an invalid argument or value, `input_required`, `session_domain_mismatch` (D2 adds `config_invalid`) |
| 3 | Try again later, nothing to fix: a rate limit or quota, a timeout or network failure on a read, a busy lock. Batch waiting. `jobs watch` ended before a terminal state |
| 4 | Check before trying again: an `unknown` outcome (an account change, or a download after the link request), a session conflict. Batch items need attention. `jobs watch` ended on `failed`, `interrupted` or `cancelled` |
| 5 | Sign-in needed: no usable session or credentials, or the session expired or was refused |
| 130 | Cancelled: Ctrl+C, EOF at a prompt, a declined confirmation, a cancelled transfer |

**Derivation.** `exit_code_for(code, outcome, quota_may_be_used, retryable)` checks these in order:

1. `cancelled` gives 130.
2. `outcome == "unknown"` or `quota_may_be_used` gives 4.
3. A fixed table:
   - 2: `invalid_argument`, `input_required`, `session_domain_mismatch` (D2: `config_invalid`).
   - 5: `upstream_authentication_required`.
   - 3: `upstream_rate_limited`, `upstream_timeout`, `upstream_network_error`, `download_unavailable`,
     `session_busy`, `client_busy`, `server_busy`, `downloads_busy`, `download_not_ready` (D2:
     `config_conflict`, `config_busy`).
   - 4: `session_conflict`, `mutation_uncertain`.
4. Otherwise 3 if `retryable`, else 1.

**Context.** `classify()` needs to know which call failed. `main()` keeps a local variable,
`operation`, that the exception handler reads:

- It is `"read"` by default.
- It becomes `"account_change"` right before `run_library_command()` runs one of the 9 mutating actions.
  For those actions, `cli_views.py` only makes the mutating Client call (section 2.1). Its exceptions are
  classified with `mutation=True`, as the HTTP API treats the whole mutating route.
- It becomes `"download"` right before `client.download()` in the single-book flow. Its exceptions are
  classified with `download=True`.
- Sign-in, session restore and searches stay `"read"`.

**Read context.** Catalog and library reads, `profile`, `quota`, `login`, `status --verify`, and local
checks:

| Condition | `code` | retryable / outcome | Exit | Hint |
|---|---|---|---|---|
| argparse usage error (`error: UsageError`) | `invalid_argument` | false / not_applied | 2 | `Run: zlib-cli COMMAND --help` |
| `ValueError` from local validation | `invalid_argument` | false / not_applied | 2 | same |
| `BooklistNotFoundError` | `booklist_not_found` | false / not_applied | 1 | `Run: zlib-cli booklists` |
| Saved session for another domain (`cli_session.py:54`) | `session_domain_mismatch` | false / not_applied | 2 | `Run zlib-cli login on that domain, or use --no-session.` |
| `download QUERY` found nothing (`cli.py:226`) | `not_found` | false / not_applied | 1 | |
| `AuthenticationError` | `upstream_authentication_required` | false / not_applied | 5 | `Run: zlib-cli login` |
| `ChallengeRequired` | `upstream_challenge` | false / not_applied | 1 | `Run: zlib-cli doctor` |
| `RateLimitError` | `upstream_rate_limited` | true / not_applied | 3 | `Check: zlib-cli quota` |
| `NetworkError`, reason `timeout` | `upstream_timeout` | true / not_applied | 3 | |
| `NetworkError`, other reasons | `upstream_network_error` | true / not_applied | 3 | `Run: zlib-cli doctor` |
| `FormatChangedError` | `upstream_protocol_changed` | false / not_applied | 1 | |
| `DownloadError` outside a download (the API refused a call) | `upstream_refused` | false / not_applied | 1 | |
| Another `ConnectorError` | `upstream_error` | false / not_applied | 1 | |
| `SessionLockTimeoutError` | `session_busy` | true / not_applied | 3 | `Try again in a moment.` |
| `SessionConflictError` | `session_conflict` | false / not_applied | 4 | `Run: zlib-cli status` |
| `SessionError` | `session_error` | false / not_applied | 1 | |
| `OSError` | `local_io_error` | false / not_applied | 1 | |
| Textual not installed (`tui`) | `missing_dependency` | false / not_applied | 1 | `python -m pip install ".[tui]"` |
| EOF, Ctrl+C, declined confirmation | `cancelled` | false / not_applied | 130 | |
| Any other exception | `internal_error` | false / not_applied | 1 | `Set ZLIB_DEBUG=1 to show details.` |

**Account-change context** (the 9 mutating actions; this is HTTP's `mutation=True` mapping):

| Condition | `code` | retryable / outcome | Exit |
|---|---|---|---|
| `MutationUncertainError`, `NetworkError`, `FormatChangedError`, another `ConnectorError` | `mutation_uncertain` | false / **unknown** | 4 |
| `RateLimitError` | `upstream_rate_limited` | false / not_applied | 3 |
| Ctrl+C during the call | `cancelled` | false / **unknown** | 130 |
| Everything else | as in the read context | | |

The hint names the read that shows the current state:

- `favorites add` and `favorites remove`: `Check with: zlib-cli favorites`.
- `tags add` and `tags remove`: `Check with: zlib-cli tags saved-ids`.
- `booklists add` and `booklists remove`: `Check with: zlib-cli booklists show BOOKLIST_ID`, with the
  validated ID.
- `booklists create`, `edit` and `delete`: `Check with: zlib-cli booklists`.

**Download context** (single-book `download`). Let `stage = error.download_stage` (absent before the link
request) and `used = may_have_used_quota(error, stage)`:

| Condition | `code` | retryable / outcome | `quota_may_be_used` | Exit |
|---|---|---|---|---|
| `DownloadError` with reason `unavailable` or `no_link`, explicitly refused (`used` false) | `download_unavailable` | true / not_applied | false | 3 |
| `DownloadError` with reason `refused` | `upstream_refused` | false / not_applied | per `used` | 1, or 4 when `used` |
| Another `DownloadError` (`invalid_book`, `format_mismatch`, `protocol`, `size_limit`, `local_write`, `save_failed`, `download`) | `download_failed` | false / not_applied, or unknown when `used` | per `used` | 1, or 4 when `used` |
| Any other error with `used` true | the cause code from the read context (for example `upstream_timeout`) | **false / unknown** | true | 4 |
| `TransferCancelled` or Ctrl+C | `cancelled` | false / not_applied, or unknown when `used` | per `used` | 130 |
| Any other error with `used` false: no stage, or an explicit refusal of the link request (`RateLimitError`, `AuthenticationError`) | as in the read context | as in the read context | false | as in the read context |

- When `used` is true, the hint is
  `The download may have used quota. Check zlib-cli quota and zlib-cli history before downloading this book again.`
- When the code is `download_unavailable`, the hint is `Check: zlib-cli quota`.
- Nothing retries automatically.

**Local API context** (`jobs` and `tui`; `queue` in D5):

- **`LocalAPIError`.** The client reads at most 64 KiB of the error body within the same deadline and
  parses `{"error": {"code", "retryable", "outcome"}}`.
  - It keeps `code` only if it matches `^[a-z][a-z0-9_]{0,63}$`, `retryable` only if it is a boolean, and
    `outcome` only if it is `not_applied` or `unknown`.
  - The server's message is never shown; the local message for the status is kept.
  - The CLI passes the code through, for example `download_not_found`, `download_conflict`,
    `downloads_unavailable`, `unauthorized` or `upstream_rate_limited`. The exit code follows the
    derivation.
  - Without a valid body, the code is `local_api_error` (exit 1).
  - Hints:
    - `unauthorized`: `Use the token file of the running server (--token-file).`
    - `downloads_unavailable`: `Start the server with --download-dir DIR.`
    - `download_conflict`: `Use a new --key for a different book.`
- **A `NetworkError` from the local API** gives `local_api_unavailable`. The hint depends on the action:

| Action | retryable / outcome | Exit | Hint |
|---|---|---|---|
| Reads (`list`, `show`, `watch`, `file`), `tui` | false / not_applied | 1 | `Start the server: zlib-cli serve --download-dir DIR` |
| `jobs add` | false / **unknown** | 4 | `Run the same command again with the same --key: the server replays it and never adds a second download.` |
| `jobs cancel` | false / **unknown** | 4 | `Check with: zlib-cli jobs show TASK_ID` |
| `jobs resume` | false / **unknown** | 4 | `Check with: zlib-cli jobs list` |

- **Unreadable token file in `tui`** (`cli_tui.py:34`): `local_api_unavailable`, exit 1.

### 4.8 Changes per command

| Command | D1 change |
|---|---|
| `download` (one book) | Result choice prompt (section 4.4). Input checks before the network. `quota_may_be_used` in errors. Success output unchanged: `{"path": "..."}` |
| The 9 mutating actions | `input_required` (exit 2, was 1) without `--yes` or input. A declined confirmation is `cancelled` (130), as JSON in JSON mode |
| `jobs watch` | Exit 0 on `succeeded`, 4 on `failed`, `interrupted` or `cancelled`, 3 when `--duration` ends first. In text mode, a last stderr note: `The task ended in state failed.` or `Stopped watching before the task finished. Run the same command to keep watching.` |
| `jobs` (all actions) | Server code pass-through. ASCII JSON and NDJSON |
| `check`, `auth-test` | Accept `--json`. It has no effect on success, which is already JSON; errors become JSON objects |
| `serve` | Sign-in input check before binding. `--json` makes errors JSON (the readiness line comes in D5) |
| `shell` | Needs a terminal unless `--interactive` is given |
| `tui` | `input_required` without a terminal; `missing_dependency` without Textual |
| `status --verify` | Exit 5 when no usable session exists (was 1) |
| `login` | Wrong credentials exit 5 (was 1) |
| root | `--version` |

### 4.9 Compatibility and migration

| Change | Who is affected | Migration note |
|---|---|---|
| Exit codes 2, 3, 4 and 5 for single commands: local validation 1 -> 2; sign-in 1 -> 5; rate limit and network errors on reads 1 -> 3; uncertain account change or download 1 -> 4 | Scripts that test `== 1` | Test `!= 0`, or use the table. Scripts that test `!= 0` are unaffected |
| `jobs watch` exits 3 or 4 instead of 0 | Scripts that chain `jobs watch && ...` | 0 now means `succeeded`. Read the last event, or test the code |
| No prompt reads piped stdin unless `--interactive` is given | `echo 1 \| zlib-cli download dune`; text piped into `shell` | Use `--pick 1`. Add `--interactive` to keep piped answers. Piped passwords were never supported |
| Account change without a terminal: `ValueError` exit 1 becomes `input_required` exit 2 | Scripts that test `== 1` | Add `--yes` |
| JSON is ASCII-escaped everywhere | Tools that read raw bytes | Parse the JSON; the values are identical |
| New error keys | None (keys are only added) | Use `code` instead of `error` |
| JSON errors where plain text was printed before: declined confirmation, EOF, Ctrl+C, `OSError`, usage errors, unexpected exceptions | Tools that read stderr text in JSON mode | Parse one JSON object |
| No traceback by default | Developers | `ZLIB_DEBUG=1` |
| Global options and `--json` accepted in any position | None (only new forms) | |
| `download --interactive` | None | It still selects the list flow and is now also accepted before the command |

### 4.10 Docs

**New `docs/cli-json.md`.** It covers the conventions of section 4.5, the error object, the code tables,
the exit codes, the input rules and the success shapes below (unchanged; examples use fake values):

| Command | stdout with `--json` |
|---|---|
| `search` | An array of Book (`title`, `authors`, `extension`, `url`, `year`, `language`, `size`, `book_id`, `book_hash`, `is_favorite`). With `--with-pagination`: `{"books": [...], "pagination": {page, limit, total_items, total_pages, next_page, previous_page}}` |
| `info` | BookDetails: Book plus `description`, `cover_url`, `identifier`, `isbn10`, `isbn13`, `publisher`, `edition`, `pages`, `categories` |
| `formats` | An array of `{book_id, book_hash, extension, size, download_supported}` |
| `download` (one book) | `{"path": "..."}` |
| `download` (list) | A batch plan or report document (`indent=2`) |
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
| `doctor` | `{results: [{domain, search_ok, checked_at, elapsed_ms, final_domain, result_count, error_code, message}], working_domains, recommended_domain}` |
| `domains` | An array of strings |
| `check` (always JSON) / `auth-test` (always JSON) | `{reachable, domain, api, search_ok, results, authenticated}` / `{"authenticated": true}` |
| `jobs add`, `show`, `cancel` / `list` / `resume` / `file` / `watch` | A task `{id, reference, state, created_at, updated_at, progress, cancel_requested, error_code, file_available, error_message, outcome}` / `{tasks, total, limit, offset, paused}` / the server's resume object (`resumed` count) / `{"path": ...}` / NDJSON events `{id, task, paused}` |
| `serve`, `shell`, `tui` | No data on stdout |

**Other docs:**

- `docs/reference.md`, "CLI behavior": replace the exit-status paragraph (`:65-68`) with the table and a
  link. Document `--no-input`, `ZLIB_NO_INPUT`, `--interactive`, `ZLIB_DEBUG` and the `PYTHONUTF8=1` tip.
- README: one short paragraph in its scripting section that links to `docs/cli-json.md`.

### 4.11 Tests (offline, synthetic)

| File | What it covers |
|---|---|
| `tests/test_error_codes.py` (new) | `classify()` in the read, mutation and download contexts, for every exception type and reason. Parity with the HTTP statuses and codes of `_upstream_error`. `may_have_used_quota` keeps batch's results |
| `tests/test_cli_contract.py` (new) | A fake Client raises each exception; the test asserts `code`, `exit_code`, `retryable`, `outcome`, `quota_may_be_used` and `hint`. JSON mode prints one stderr line and an empty stdout. Text mode keeps `TypeName: message` as its first line. Usage errors in JSON (`domains --bogus --json`). Declined confirmation, EOF and Ctrl+C. `OSError`. Unexpected exceptions without a traceback, and with one under `ZLIB_DEBUG=1`. `--version`, including `PackageNotFoundError`. Every new message and hint is ASCII |
| `tests/test_cli_input.py` (new) | Every prompt site from section 4.4, for each policy (no terminal, `--no-input`, `ZLIB_NO_INPUT=1`, `--interactive`, a terminal). A stdin stub fails if it is read. `http.client.HTTPSConnection` is patched to fail. `getpass` must never be called without a terminal. A Windows `NUL` stream (`isatty` true, no console mode) counts as not a terminal. `serve` fails before `server_factory` is called. The choice loop: `abc` then `2` picks 2; `q` and EOF exit 130 |
| `tests/test_cli_parser.py` (new) | Global options before and after the command and after an action. The same namespace for `--json favorites add X --yes` and `favorites add X --yes --json`. Conflicts across levels. The 38 example lines of section 11.1 still parse |
| `tests/test_cli_encoding.py` (new) | stdout and stderr as `io.TextIOWrapper(io.BytesIO(), encoding="cp1252")`. A Cyrillic title in text and JSON mode: no crash, and the JSON parses back to the same title |
| `tests/test_cli_jobs.py` (extended) | `jobs watch` exits 0, 3 and 4. Server code pass-through. A body that is too large, invalid or carries a bad code gives `local_api_error`. A network failure on `jobs add` gives `unknown` and 4 |

Existing tests that answer prompts through a `StringIO` stdin must now patch `cli_terminal.is_terminal` to
return `True`, or pass `--interactive`. This is expected test churn, not a behavior regression.

### 4.12 Acceptance criteria (D1)

- [ ] Every failure path in JSON mode writes exactly one JSON object line on stderr, with the keys of
  section 4.6. stdout stays empty, except for the data-bearing partial results.
- [ ] Exit codes follow section 4.7 and appear in the root `--help` epilog and in `docs/cli-json.md`.
- [ ] `classify()` is the only mapping. `http_api/server.py` uses it, and the HTTP tests pass unchanged.
- [ ] No prompt reads stdin or calls `getpass` when input is not allowed. Each refusal happens before any
  network request and names the missing option.
- [ ] `serve` refuses before binding when it would need to prompt.
- [ ] `search`, `search --json` and the other listings work on a cp1252 stdout with a non-ASCII title, and
  the JSON round-trips.
- [ ] `--json`, `--no-input`, `--interactive`, `--domain`, `--credentials`, `--timeout`, `--session-file`
  and `--no-session` work before and after the command. Conflicts are refused at any position.
- [ ] `zlib-cli --version` prints `zlib-cli <version>` and exits 0.
- [ ] `jobs watch` exits 0, 3 or 4, and `jobs` errors carry the server's code.
- [ ] Docs and migration notes are written. CI is green on Windows and Linux with Python 3.11 and 3.14.

If review finds D1 too large for one PR, it splits at one point only: first the output contract (sections
4.5 to 4.7, `--version` and global options), then the input policy (section 4.4). Each half stays one
commit.

### 4.13 Implementation notes (2026-11-12)

D1 shipped as one commit, `Add scriptable CLI contract`. Where it differs from the text above:

- **Account-change context follows the HTTP mapping exactly.** Any failure of the mutating call that is not an
  explicit refusal (sign-in, challenge, rate limit, `DownloadError`, booklist not found) is `mutation_uncertain`
  with `outcome: "unknown"`, including a local `ValueError`, `OSError` or session error. A `DownloadError` stays
  `upstream_refused`, as in HTTP.
- **HTTP keeps its vocabulary.** `upstream_error()` calls `classify()` and maps the code to a status and fixed
  message. Codes that HTTP never returned (`session_busy`, `invalid_argument`, `cancelled`, ...) fall back to
  `upstream_error`, `internal_error` or, for a mutation, `mutation_uncertain`, so every HTTP and MCP answer is
  unchanged. The MCP server already used `upstream_error()` and therefore shares `classify()`.
- **Text lines kept as before** for a cancellation (`Cancelled.`, `Cancelled. Nothing was changed.`) and a local
  I/O failure, without a `TypeName:` prefix. A hint already contained in the message is not repeated.
- **`download --interactive` selects the list flow**, as section 4.3 requires, so piped answers for one title go
  through the one-book list flow (a choice, then a confirmation).
- **`-a -` without a saved session or email** keeps its message and becomes `input_required` (exit 2).
- **`status` and `logout`** use `session_domain_mismatch` for their domain check too.
- **Parser.** The global options are added to every command and action parser by walking the subparsers, so the
  command modules only lost their own `--json`. The root parser fills `GLOBAL_DEFAULTS` itself, so other callers of
  `parser()` (the MCP test fixture, `shell`) see the defaults.
- **`zlib-cli mcp` startup refusals** keep their one-line text and exit 1: the MCP server owns stdout and its own
  contract (`docs/mcp.md`). The `--print-config` snippets are unchanged.

---

## 5. D2: settings and `config` (full spec)

### 5.1 Scope

**In scope:**

- `zlib_connector/settings.py`, the single resolver;
- the config file;
- `ZLIB_*` environment variables;
- the `config` command;
- the credentials lookup in the config folder;
- `serve --downloads`;
- `serve` listening on the configured `api_url`;
- the `doctor` hint.

**Out of scope:**

- `proxy` (D7) and `progress` (D3);
- keys for the session or token file (not planned);
- stdin lists (D4 and D5).

### 5.2 Files and locations

| File | Windows | Linux and macOS | Override |
|---|---|---|---|
| `config.toml` | `%APPDATA%\zlibrary-api\config.toml` (`~\AppData\Roaming` when `APPDATA` is unset) | `$XDG_CONFIG_HOME/zlibrary-api/config.toml` when that variable is absolute, else `~/.config/zlibrary-api/config.toml` | `--config FILE`, then `ZLIB_CONFIG`. `--no-config` ignores the file for one run |
| `credentials.toml` | Same folder | Same folder | `--credentials FILE`, `ZLIB_CREDENTIALS`, or the config key `credentials_file` |
| `config.toml.lock` | Beside the config file | Beside the config file | |
| `session.json`, `api-token`, job store | Unchanged: the state directory (`ZLIB_STATE_DIR`, `%LOCALAPPDATA%\zlibrary-api`, `${XDG_STATE_HOME:-~/.local/state}/zlibrary-api`) | | `--session-file`, `--token-file` |

Rules:

- `--config FILE` and `--no-config` join D1's `common_options()`, so they work in any position. They
  exclude each other at any position (exit 2).
- A config file named by `--config` or `ZLIB_CONFIG` must exist (`config_invalid`, exit 2). The exception
  is the `config` subcommands: they treat a missing file as empty, and `config set` creates it. Its parent
  folder must exist.
- The default folder is created only by `config set` and `config unset`, with mode `0o700`.
- `config` subcommands refuse `--no-config` (exit 2).

### 5.3 File format

- TOML 1.0, UTF-8 (a BOM is accepted), at most 64 KiB, one flat table.
- **Strict reading.** These fail with `config_invalid` (exit 2):
  - an unknown key, a nested table or a wrong type;
  - a key that looks like a secret: `email`, `password`, `token`, `api_token`, `cookie`, `cookies`,
    `session`, `secret`. The message is `Credentials and session data are never stored in the config
    file.`

  Messages name the file, the key and the source. They never show a value. Keys are shown through
  `terminal_text()`, at most 64 characters.

Example (written by `config set`):

```toml
# zlib-cli settings. "zlib-cli config set" rewrites this file and drops comments.

domain = "https://z-lib.by/"
timeout = 60
download_dir = "C:\\Users\\me\\Books"
languages = ["english", "french"]
formats = ["epub"]
```

### 5.4 Keys (version 1)

| Key | TOML type | Validation (same validators as at run time) | CLI | Environment | Default | Used by |
|---|---|---|---|---|---|---|
| `domain` | string | `session_data.canonical_origin()`. Stored canonical (`https://host/`) | `--domain` | `ZLIB_DOMAIN` | unset (section 5.5) | every site command except `doctor`, `status` and `logout` |
| `timeout` | integer or float | finite, more than 0, at most 300 | `--timeout` | `ZLIB_TIMEOUT` | 30 | site commands, `doctor`, `serve`, local API clients |
| `download_dir` | string | `~` is expanded. Must be absolute. `config set` requires an existing folder | `download --output`, `jobs file --output` | `ZLIB_DOWNLOAD_DIR` | the current directory | single and list downloads, `jobs file`, `serve --downloads` |
| `languages` | array of strings | `search._choices` rules: 1 to 64 printable characters each, at most 50, lowercased, deduplicated | `--language` (repeatable) | `ZLIB_LANGUAGES` (comma-separated) | none | `search`, and `download` when it searches |
| `formats` | array of strings | a subset of `pdf`, `epub`, `djvu` | `--format` (repeatable) | `ZLIB_FORMATS` (comma-separated) | none (all supported) | `search`, and `download` when it searches |
| `api_url` | string | `local_api._address()` rules (loopback HTTP, optional `/v1`, port not 0). Stored as `http://HOST:PORT/v1` | `--api-url` (`jobs`, `tui`) | `ZLIB_API_URL` | `http://127.0.0.1:8000/v1` | `jobs`, `tui`, the `serve` listen address |
| `credentials_file` | string | `~` is expanded. Must be absolute and must exist when used | `--credentials` | `ZLIB_CREDENTIALS` | lookup (section 5.9) | account sign-in |

**Never stored**, on purpose: email, password, cookies, the API token, `--yes`, `--pick`, `--json`,
`--no-input`, and the switch that enables downloads on `serve`.

### 5.5 Precedence

**General rule, for every key:**

1. A CLI option given in this run, or an explicit argument to `load_settings(overrides)`.
2. The environment variable, when it is non-empty after trimming. An empty value counts as unset.
3. The config file, unless `--no-config` is given.
4. The built-in default.

- **Lists replace each other** level by level and never merge.
- **Environment lists** are comma-separated; items are trimmed and empty items dropped.
- **Invalid values** fail before any network request. A bad environment or file value gives
  `config_invalid` (exit 2); the message names the key and its source (`ZLIB_TIMEOUT`, or the file and
  key), never the value. A bad CLI value keeps today's `invalid_argument` (exit 2).
- **`--no-config`** ignores the file only. Environment variables still apply.

**Domain chain for account commands** (`login`, `auth-test`, `profile`, `quota`, `library`, `favorites`,
`history`, `booklists`, `tags`, `download`, `shell`, `serve`, and `search`, `info`, `formats`, `similar`,
`popular` and `recommended` without `--anonymous`):

1. `--domain`
2. `ZLIB_DOMAIN`
3. The config key `domain`
4. The `domain` of a **named** credentials file (`--credentials`, `ZLIB_CREDENTIALS` or `credentials_file`),
   only if the file sets `domain`
5. The saved session's origin
6. The `domain` of the default credentials file (`<config dir>/credentials.toml`, else the legacy file), if
   it sets one
7. `https://z-lib.by/`

- When a saved session will be restored and levels 1 to 4 chose another domain, the command fails with
  `session_domain_mismatch` (exit 2). The message names the source, for example: `The saved session
  belongs to another domain than the one set by the config file (domain). Run zlib-cli login to sign in
  there, or use --no-session.` This is today's `--domain` rule, extended.
- **Fixed quirk:** a named credentials file without `domain` no longer forces `https://z-lib.by/` over the
  saved session's domain. `load_credentials()` records whether the file set `domain` (new field
  `has_domain`).

**Other chains:**

- **Anonymous chain** (`--anonymous`, `check`, `serve --anonymous`): `--domain`, `ZLIB_DOMAIN`, config
  `domain`, `https://z-lib.by/`. The session and credentials are not read, as today.
- **`doctor`** probes its positional domains, else `--domain`, else the known candidates. It ignores the
  environment and config domain, because its job is to find working domains. In text mode:
  - it prints `Configured domain: https://... (config)` when one is set;
  - when the recommended domain differs from the configured one, it prints
    `To use it: zlib-cli config set domain https://.../`.

  Its JSON output does not change.
- **`status` and `logout`** compare the saved session only with an explicit `--domain`, as today. When the
  environment or config domain differs from the session's origin, they print a stderr note and still
  exit 0.

### 5.6 Notices (stderr, once per run)

These are messages, not data, so they always go to stderr, JSON mode included. D3's `--quiet` hides them.

| When | Text |
|---|---|
| A search runs with formats or languages from the environment or the config file | `Using formats from the config file: epub. Use --format to replace them, or --no-config to ignore the file.` / `Using languages from ZLIB_LANGUAGES: english, french.` |
| The legacy credentials file is read | `Note: using credentials.local.toml beside the installed package. Move it to <config dir>/credentials.toml (zlib-cli config path shows the folder).` |
| An environment variable overrides a value that `config set` just wrote | `Note: ZLIB_FORMATS is set and overrides this value in this environment.` |
| `config set` or `config unset` dropped comments | `Note: comments in the config file were not kept.` |
| `config set domain` with a session saved for another domain | `Note: the saved session belongs to https://z-lib.sk/. Account commands will stop with an error until you run zlib-cli login.` |

Filters never apply to direct references. `download ID:HASH` with configured formats works and prints no
notice: today's rule "filters cannot be used with a direct reference" applies only to filters given on the
command line. Batch items with exact references are unaffected too.

### 5.7 The `config` command

```text
zlib-cli config path   [--json]
zlib-cli config list   [--show-origin] [--json]
zlib-cli config get    KEY [--json]
zlib-cli config set    KEY VALUE... [--json]
zlib-cli config unset  KEY [--json]
```

- `config` never contacts the network. It never reads the credentials file or the API token. It reads the
  session only for the `config set domain` note.
- `KEY` is one of the 7 keys. It is validated by hand rather than with argparse `choices`, so that
  secret-looking keys get their specific message.
- Every subcommand works whether or not the config file exists (section 5.2).

**`config path`**

- Text: the config file path on stdout. When the file is missing, stderr adds
  `(not created yet; zlib-cli config set creates it)`.
- JSON:

  ```json
  {"path": "C:\\Users\\me\\AppData\\Roaming\\zlibrary-api\\config.toml", "exists": false, "source": "default", "credentials_path": "C:\\Users\\me\\AppData\\Roaming\\zlibrary-api\\credentials.toml", "credentials_exists": false}
  ```

  - `source` is `cli`, `env` or `default`.
  - `credentials_path` is the file the lookup would use, or `null` when none exists.

**`config list`**

- Text, one aligned row per key. `(not set)` stands for null or an empty list. `--show-origin` adds a third
  column (`default`, `config`, `env ZLIB_LANGUAGES`, `cli --timeout`):

  ```text
  domain            (not set)                  default
  timeout           60                         config
  download_dir      C:\Users\me\Books          config
  languages         english, french            env ZLIB_LANGUAGES
  formats           epub                       config
  api_url           http://127.0.0.1:8000/v1   default
  credentials_file  (not set)                  default
  ```

- JSON always includes the origin. `option` appears for `cli` and `variable` for `env`:

  ```json
  {"path": "...config.toml", "exists": true, "settings": {"domain": {"value": null, "origin": "default"}, "timeout": {"value": 60, "origin": "config"}, "download_dir": {"value": "C:\\Users\\me\\Books", "origin": "config"}, "languages": {"value": ["english", "french"], "origin": "env", "variable": "ZLIB_LANGUAGES"}, "formats": {"value": ["epub"], "origin": "config"}, "api_url": {"value": "http://127.0.0.1:8000/v1", "origin": "default"}, "credentials_file": {"value": null, "origin": "default"}}}
  ```

- An unset `domain` means "levels 4 to 7 of the chain decide". `config list` does not resolve those levels,
  because it does not read credentials or the session.

**`config get KEY`**

- Text: a scalar on one line, a list one item per line, nothing when unset. Exit 0.
- JSON: `{"key": "formats", "value": ["epub"], "origin": "config"}`.

**`config set KEY VALUE...`**

- A scalar key takes exactly one value; otherwise it is a usage error (exit 2).
- A list key takes one or more values. A comma-separated value is also accepted: `formats epub,pdf` equals
  `formats epub pdf`.
- `timeout` is read as an integer when possible, else as a float.
- Values are normalized before writing: the canonical domain, absolute paths, lowercased lists.
- Text: `Set formats = epub, pdf in <path>.`, plus any notices from section 5.6.
- JSON:

  ```json
  {"key": "formats", "value": ["epub", "pdf"], "previous": null, "path": "..."}
  ```

- An empty list cannot be set; `config unset` clears the key.

**`config unset KEY`**

- Text: `Removed formats from <path>.`, or `formats was not set in <path>.` Both exit 0.
- JSON: `{"key": "formats", "removed": true, "previous": ["epub"], "path": "..."}`.

**Repairing a bad file.** `config set KEY` and `config unset KEY` also accept a file whose only problem is
the value of that same key, so a bad value can be fixed. Any other problem, such as invalid TOML or
another bad key, is refused with
`Fix or remove the config file first: <path>`.

**New error codes (D2):**

| `code` | Cause | Exit |
|---|---|---|
| `config_invalid` | Invalid file, key or value (CLI, environment or file). The error object adds `key` and `source` | 2 |
| `config_conflict` | The file changed while it was being updated (section 5.8). Retryable | 3 |
| `config_busy` | Lock timeout (5 s). Retryable | 3 |
| `local_io_error` | The file cannot be read or written | 1 |

Example: `{"error": "SettingsError", "message": "The config file has an invalid value for timeout.",
"code": "config_invalid", "exit_code": 2, "retryable": false, "outcome": "not_applied", "key": "timeout",
"source": "config", "hint": "Run: zlib-cli config unset timeout"}`.

**Confirmation rules.** `config set` and `config unset` change only a local file. They are reversible and
print what they changed, so they need no prompt and no `--yes`, like `git config`. They never change the
Z-Library account. `config set domain` warns when the new domain differs from the saved session's domain.

### 5.8 Protected writes

`ConfigStore.update(key, value_or_UNSET)` reuses the session patterns from `sessions.py`:

1. **Path checks.** Resolve the path. Refuse it when it is a link (`lstat`) or exists and is not a regular
   file: `config set refuses a linked config file; edit its target directly.` Reading follows links, so
   dotfile setups work; writing never replaces a link. Create the default folder (`0o700`, parents) if
   needed.
2. **Lock.** Acquire `session_lock.private_lock(<file>.lock, 5.0)`. A timeout gives `config_busy`.
3. **Read.** Read the current bytes (empty when missing) and compute `generation = sha256(bytes)`. Parse
   them strictly (the repair exception of section 5.7 applies). Apply the change and serialize.
4. **Write.** Write the new content to a private temporary file in the same folder
   (`private_files.create_private_file`), then flush and fsync.
5. **Compare.** Under the lock, read the file again. A different generation gives `config_conflict`, and
   the temporary file is removed.
6. **Replace.** `os.replace(temp, path)`, with the same bounded retry as `sessions.py:201-231` for Windows
   errors 5 and 32. Remove the temporary file on any failure.

**TOML writer** (`settings.dumps_config`):

- Output: a fixed header comment, a blank line, then the set keys in the order of section 5.4.
- Strings use TOML basic strings, and the file is ASCII. These are escaped: `\` as `\\`, `"` as `\"`,
  U+0000 to U+001F and U+007F as `\uXXXX`, and non-ASCII characters as `\uXXXX` or `\UXXXXXXXX`.
- Integers use `str()`. Floats use `repr()` and are always finite.
- Arrays of strings are written as `["a", "b"]`.
- Booleans are written as `true` or `false` (no version 1 key uses them).
- A test checks `tomllib.loads(dumps_config(v)) == v` for generated values: quotes, backslashes, Windows
  paths, non-ASCII text and control characters.
- The file is private after `config set`, because `create_private_file` creates it. A hand-written file is
  not required to be private: it holds no secrets.

### 5.9 Credentials lookup

1. A named file: `--credentials` (a relative path resolves against the current directory), else
   `ZLIB_CREDENTIALS`, else `credentials_file`. It must exist (exit 2, as today for `--credentials`).
2. `<config dir>/credentials.toml`, if it exists.
3. The legacy `credentials.local.toml` beside the package (`credentials.py:6`), if it exists. A notice is
   printed (section 5.6).
4. None: the CLI prompts (D1 input rules).

Code changes:

- `credentials.py` gains `credentials_path(explicit=None) -> Path | None`. `DEFAULT_FILE` stays as the
  legacy constant for compatibility.
- `load_credentials(path=None)` uses the lookup and adds `has_domain`. It never prints; the CLI prints the
  notice.
- `cli_batch.py:383` checks that the list is not the credentials file against the resolved path.
- `credentials.example.toml` and the docs point to `<config dir>/credentials.toml`. Signing in with
  `login` stays the recommended way.

### 5.10 `serve`, `jobs` and `tui`

- **`serve --downloads`** enables download tasks in the resolved `download_dir` (environment or config).
  - Without one, it fails: `No download folder is configured. Use --download-dir DIR, or zlib-cli config
    set download_dir DIR.` (`invalid_argument`, exit 2).
  - `--downloads` and `--download-dir` exclude each other (exit 2).
  - `--download-dir DIR` works as before.
  - A configured `download_dir` never enables tasks on its own: spending quota stays an explicit choice at
    each launch.
- **Listen address.** When `--host` and `--port` are not given and `api_url` comes from the environment or
  the config file, `serve` listens on that URL's host and port. `jobs` and `tui` then reach it without
  options.
- **Defaults.** `jobs --api-url` and `tui --api-url` default to the resolved `api_url`. `jobs file --output`
  defaults to `download_dir`, else the current directory.

### 5.11 Python API

```python
from zlib_connector.settings import Settings, SettingsError, load_settings

settings = load_settings(overrides={"timeout": 10}, environ=None, config_file=None, use_config=True)
settings.timeout              # 10.0
settings.origins["timeout"]   # ("cli", None); ("env", "ZLIB_TIMEOUT"); ("config", None); ("default", None)
```

- `load_settings(overrides=None, *, environ=None, config_file=None, use_config=True) -> Settings`.
  - `environ` defaults to `os.environ`.
  - `config_file` stands in for `--config`.
  - In `overrides`, a `None` value means "not given".
- `Settings` is a frozen dataclass:
  - fields `domain: str | None` (levels 1 to 3 only), `timeout: float`, `download_dir: Path | None`,
    `languages: tuple[str, ...]`, `formats: tuple[str, ...]`, `api_url: str` and
    `credentials_file: Path | None`;
  - plus `config_path: Path | None`, `config_exists: bool` and `origins`.
- `SettingsError(ValueError)` has the attributes `code`, `key` and `source`.
- `load_settings()` never prints, never touches the network and never reads credentials, the session or the
  token.
- `Client`, `SessionStore` and `DownloadManager` stay explicit: they never read the environment or the
  config file.
- `zlib_connector/__init__.py` exports `load_settings`, `Settings` and `SettingsError`.
- The CLI, `serve`, the TUI launcher and the MCP server all use `load_settings()`.

### 5.12 Wiring in `main()`

1. Parse; the D1 steps run first.
2. `config` subcommands run with their own settings load.
3. Every other command runs `load_settings()` once. Argparse defaults become `None` for `--timeout`,
   `download --output`, `jobs file --output` and `--api-url`, so that an explicit option can be told apart
   from a default.
4. Fill `args` from the settings: the timeout, the output folder, the API URL, the credentials path and the
   filters (only for searches, never for direct references). Keep the domain and its origin for the
   messages.
5. Build the input policy and dispatch.

`--help`, `--version` and (in D6) `__complete` never load settings.

### 5.13 Compatibility and migration

| Change | Impact | Note |
|---|---|---|
| No config file and no `ZLIB_*` variables | None: identical behavior | |
| The `ZLIB_*` variables now take effect | Shells that already define e.g. `ZLIB_DOMAIN` for something else | Release note |
| Credentials lookup: the config folder first, then the legacy file with a notice | None for existing setups (the new file does not exist yet) | Move the file when convenient. The legacy path keeps working |
| A named credentials file without `domain` no longer forces `https://z-lib.by/` over the session domain | Fixes needless mismatch errors | Release note |
| `--timeout`, `--output` and `--api-url` defaults come from settings | Same values by default | |
| `serve --downloads` | New | |
| `serve` listens on a configured `api_url` | Only when `api_url` is configured | Pass `--host` and `--port` to override |

### 5.14 Docs

- New `docs/configuration.md`: file locations, the keys table, precedence and the domain chain, the
  environment variables, `config` examples, the credentials file, and the MCP environment note (section 9).
- README: a short "Save your preferences" section (`config set formats epub`, `config set download_dir`).
- `docs/reference.md`: links. `credentials.example.toml`: its location comment.

### 5.15 Tests (offline, synthetic, temporary folders, patched `environ`)

| File | What it covers |
|---|---|
| `tests/test_settings.py` (new) | Default locations on Windows (`APPDATA` set and unset) and POSIX (`XDG_CONFIG_HOME` absolute, relative or unset), using a minimal environment such as an MCP client passes (only `APPDATA` or `HOME`). The precedence matrix for every key (CLI > environment > file > default). An empty environment value counts as unset. Lists replace. `--no-config` ignores only the file. Invalid values from each source give `config_invalid` naming the key and source without the value. Strict reading: unknown key, secret-looking key, nested table, wrong type, relative path, BOM, more than 64 KiB, invalid TOML (the content is never shown). Writer round-trip through `tomllib` |
| `tests/test_cli_config.py` (new) | `config path`, `list` (with and without `--show-origin`), `get`, `set`, `unset`: text and JSON. The folder is created with `0o700` on POSIX and the file is private. A linked config file is refused. `config_conflict` when the file changes between read and replace. `config_busy` on lock timeout. The comment, environment-override and session-domain notes. Repair of a bad value of the same key. `--no-config` is refused. The credentials file and token are never read: patch `load_credentials` and `read_api_token` with `AssertionError` |
| `tests/test_cli_sessions.py` (extended) | The 7-level domain chain, the mismatch message for each source, the `has_domain` fix. `status` and `logout` print a note instead of failing |
| `tests/test_cli_settings_use.py` (new) | The filter notice is printed once; not with CLI flags; not for `download ID:HASH`, which succeeds with configured formats. `download_dir` is the default output for one book, for lists and for `jobs file`. A missing configured folder gives `config_invalid`. `doctor` ignores the environment and config domain and prints the `config set domain` hint. `serve --downloads` with and without a configured folder; `--downloads` with `--download-dir` is refused; a configured folder alone never enables tasks; `serve` listens on the configured `api_url`. The legacy credentials notice. The batch credentials-file guard uses the resolved path |
| `tests/test_credentials.py` (extended) | The lookup order and `has_domain` |

### 5.16 Acceptance criteria (D2)

- [ ] With no config file and no `ZLIB_*` variables, every existing test passes unchanged.
- [ ] `load_settings()` resolves the 7 keys with the precedence of section 5.5 and records their origins.
  The CLI, `serve` and the TUI launcher use it. Constructors do not read settings.
- [ ] The config file is found at the documented default location on Windows and POSIX, including with a
  minimal environment (`APPDATA` or `HOME` only).
- [ ] `config path`, `list`, `get`, `set` and `unset` work in text and JSON, with the shapes and exit codes
  of section 5.7.
- [ ] Writes are locked, compare generations, go through a private temporary file and are atomic. Links are
  refused for writing. Secret-looking keys are refused.
- [ ] Configured filters are announced on stderr, never apply to direct references and never choose an
  edition.
- [ ] The domain chain is implemented, with mismatch messages that name their source. `doctor`, `status`
  and `logout` follow section 5.5.
- [ ] `serve --downloads` needs a configured folder, and a configured folder never enables downloads on its
  own.
- [ ] Docs, migration notes and CI green on the 4 configurations.

### 5.17 Implementation notes (2026-11-12)

D2 shipped as one commit, `Add CLI settings`, on top of D1, the MCP server and the TUI. Where it differs from the
text above:

- **Docs.** No new `docs/configuration.md`: the settings are a "Settings" section of `docs/reference.md` (locations,
  keys, precedence, domain chain, credentials lookup, `config`, Python), the JSON shapes and codes are in
  `docs/cli-json.md`, and the README has a short "Save your preferences" section. `docs/mcp.md` and
  `credentials.example.toml` point to them.
- **Modules.** `settings.py` (resolver, strict reader, writer, `ConfigStore`), `cli_settings.py` (one load per run,
  argument filling, once-per-run notices, configured filters), `cli_config.py` (the command). `local_state.py` gains
  `default_config_directory()` and `config_directory()`; `credentials.py` gains `credentials_path()`,
  `default_credentials_file()` and `Credentials.has_domain` (default `True`, so code that builds `Credentials` by
  hand keeps today's chain).
- **Origins.** `Settings.origins["timeout"]` is `("cli", "--timeout")` rather than `("cli", None)`, so `config list
  --show-origin` and the JSON `option` field can name the option.
- **CLI values are validated by the resolver** (`invalid_argument`, exit 2, `source: "cli"`), including commands that
  ignored them before (`zlib-cli --timeout 0 domains` now exits 2). An explicit `--domain` and `--api-url` are passed
  on as typed; environment and file values are normalized.
- **`credentials.toml` lives in the default settings folder** even when `--config` or `ZLIB_CONFIG` names a file
  elsewhere. `config path --json` reports as `credentials_path` the file a sign-in would read, else the default
  place for one, with `credentials_exists`.
- **Notices.** The filter notice is printed just before the search request (after local checks), so a run refused by
  a local check shows only its error object. The `status` and `logout` note names the source of the configured
  domain. `shell` searches do not use configured filters.
- **Lists.** Configured filters join the batch filters only when the list has searched items. A reference item whose
  format is not among them gets the existing informational line ("Search filters apply only to searches; ...");
  nothing is refused or chosen.
- **`serve`.** `--downloads` and `--download-dir` are an argparse mutually exclusive group (exit 2). `--host` and
  `--port` keep their documented defaults; a marker type tells the default apart, so an explicit
  `--port 8000` still wins over `api_url`.
- **MCP.** `zlib-cli mcp` loads the settings itself (its tests call `run_mcp` directly). A settings error is its usual
  one-line refusal, exit 1, as D1 kept for every MCP startup refusal. The domain and timeout follow section 5.5; a
  named or configured credentials file is ignored (MCP never reads credentials) and only `--credentials` is refused.
  `--print-config` embeds `--domain` only when given on the command line, adds `--config FILE` or `--no-config`
  when given, and copies `ZLIB_CONFIG`, `XDG_CONFIG_HOME`, `ZLIB_DOMAIN` and `ZLIB_TIMEOUT` when set.
- **TUI.** It talks only to the local API, so it uses `api_url` and `timeout`.
- **Tests.** `tests/session_fixture.py` points `config_directory()` and the legacy credentials file at a temporary
  folder for the whole test process and removes the `ZLIB_*` settings variables, so a developer's own settings never
  reach the suite. Subprocess tests inherit the cleaned environment.

---

## 6. Later deliverables (scope and acceptance criteria)

### D3. Help, examples and tables

- [x] **Root help.** `zlib-cli --help` groups the commands (`metavar="COMMAND"`, a description with
  `RawDescriptionHelpFormatter`):

  ```
  Find:      search info formats similar recommended popular recent
  Download:  download queue jobs
  Library:   library favorites history booklists tags
  Account:   login logout status profile quota
  Site:      doctor domains
  Tools:     config serve tui shell completion mcp
  ```

  - The groups list only commands that exist at the time.
  - The epilog gives the exit codes, the config file location and the sentence `Scripts should use --json;
    text output may change.`
- [x] **No command or a mistyped command.** With no command, the grouped help goes to stderr (exit 2). A
  mistyped command gets `Did you mean: search?` from `difflib` (exit 2); `suggest_on_error` does not exist
  on Python 3.11.
- [x] **Command help.** Every command has a summary, a one-paragraph description and 2 to 4 examples under
  `Examples:`.
  - The examples live in one table, `zlib_connector/cli_examples.py`.
  - A test parses every example with `parser()` and expects no error and no leftover argument.
- [x] **Option help.** Every option has help text.
  - `--order` explains each token. `title` sorts Z to A, `titleA` A to Z, `filesize` largest first,
    `filesizeA` smallest first; `docs/reference.md` has the full list.
  - The metavars are `ID:HASH`, `BOOKLIST_ID`, `ENTRY_ID` and `TASK_ID`.
- [x] **ASCII text.** A test checks that every help string and every message the CLI writes itself is
  ASCII. The en dashes become `to`, and the `·` separators become two spaces.
- [x] **Tables.** `zlib_connector/cli_table.py` renders them with the standard library:
  - one header row, and columns separated by two spaces;
  - widths from `shutil.get_terminal_size()` only when stdout is a terminal;
  - only the title and author columns are truncated, with `...`;
  - references are never truncated, so they can be copied;
  - every cell goes through `terminal_text()`;
  - one line per book;
  - used by search, discovery, favorites, history, booklists, `booklists show`, formats, `jobs list` and
    `doctor`.

  ```
   #  TITLE                    AUTHORS         YEAR  LANG     EXT   SIZE    REFERENCE
   1  Dune [saved]             Frank Herbert   1965  english  EPUB  1.2 MB  1234567:a1b2c3
  ```
- [x] **Profile.** `profile` prints labeled sections, so it no longer looks the same as `quota`.
- [x] **Quiet and progress options.** A global `--quiet` hides notes and progress but keeps errors and data.
  `--progress` and `--no-progress` become global. The `progress` key (`auto`, `always`, `never`) and
  `ZLIB_PROGRESS` are registered in `settings.KEYS`.
- [x] **`doctor` progress.** `doctor` shows `Checked N of M` in a terminal, with the batch ticker.
- [x] **Deprecated commands.** `check` and `auth-test` are hidden from help. Their behavior and JSON do not
  change, and each prints one deprecation line on stderr: `check` points to `doctor DOMAIN`, `auth-test`
  to `login` then `status --verify`. They can be removed no earlier than one release after the notice.

### D4. Clear account changes, dry runs and lists

- [x] **Described confirmations.** The prompt names the action, the book title (read once when needed), the
  booklist title and its book count, and the domain. A deletion also says `This cannot be undone.`:
  `Permanently delete booklist 98765 "To read" (12 books) on https://z-lib.by/? This cannot be undone. [y/N]`.
  - For `booklists remove`, the entry's book is looked up in the list's first pages. If it is not found,
    the prompt says `entry 4242 (book not identified)`.
- [x] **`--dry-run`.** All 9 mutating actions accept `--dry-run`.
  - It validates the change and resolves it, with reads allowed.
  - It prints the change that would be sent: `{"dry_run": true, "action": "favorite_add", "domain": ...,
    "book_id": ..., "title": ...}`.
  - It never prompts, sends no change and exits 0.
- [x] **Lists of references.**
  - `favorites add -` and `favorites remove -`, `booklists add BOOKLIST_ID -`, and `tags add - TAG...` and
    `tags remove - TAG...` read a list from stdin. `-a FILE` reads the same list from a file.
  - Both reuse `batch_input.parse_input`.
  - Only exact references are accepted: `ID:HASH`, a book URL, or objects with `book_id` and `book_hash`.
    The JSON output of `search`, `favorites` and `booklists show` therefore works as input.
  - A title is refused with a pointer to `download -a LIST --dry-run --json`.
- [x] **List changes.**
  - A list change requires `--yes`.
  - It first prints a stderr summary such as `Add 12 books to favorites on https://z-lib.by/`.
  - It returns `{"items": [{"position", "reference", "status", "code"}], "summary": {"applied", "failed",
    "uncertain", "not_started"}}`, in input order.
  - Exit 0 when every item was applied, 4 when any item failed or is uncertain, and 3 when a rate limit
    stopped the run (the remaining items are `not_started`).
  - No item is ever retried.
- [x] **Login notice.** `login` prints a notice when it replaces a session saved for another domain.
  `logout` keeps saying that it does not revoke the server session.
- Implementation notes (2026-11-13): the description needs reads, so a described prompt now comes after
  sign-in; `--yes` skips those reads. A sign-in or verification failure also stops a list. `--dry-run` with
  `--yes` is refused.

### D5. `queue`, `recent` and `serve --json`

- [ ] **`queue add`** takes `REF...`, `-` or `-a FILE`, with `--key PREFIX`.
  - It sends one `POST /v1/downloads` per item, with keys `PREFIX-0001`, `PREFIX-0002`, and so on. The
    derived keys must pass `validate_idempotency_key`: 8 to 128 characters from letters, digits, `.`,
    `_`, `~` and `-`.
  - Only exact references are accepted.
  - It returns `{"items": [{"position", "reference", "key", "task_id", "state", "code"}], "summary"}`.
  - Running the same command again replays the same submissions, because the server persists each key
    before it acknowledges the request. Exit codes follow D4.
- [ ] **`queue list` and `queue resume`** are the same calls as `jobs list` and `jobs resume`, with the same
  JSON. `jobs add`, `jobs list` and `jobs resume` keep working. `queue watch` waits for a global event
  route.
- [ ] **`recent [--limit N]`** is built on a recorded fixture of `/eapi/book/recently` that holds a book
  list.
  - If the endpoint returns no list, the command fails with `upstream_unavailable` (exit 1). It never
    prints an empty list, which would read as "nothing new".
  - Its help points to `search QUERY --order date`.
  - This depends on open question 6.
- [ ] **`serve --json`** prints one NDJSON line on stdout once the server listens:
  `{"event": "listening", "url": "http://127.0.0.1:PORT/v1", "token_file": "...", "downloads": true}`. It
  never prints the token.

### D6. Shell completion (standard library only)

- [ ] **Glue scripts.** `zlib-cli completion bash`, `zsh`, `fish` or `powershell` prints a glue script.
  The CLI never edits a shell profile; the docs give the line to add:

  | Shell | Line to add | Mechanism |
  |---|---|---|
  | bash | `eval "$(zlib-cli completion bash)"` in `~/.bashrc` | `complete -F` (https://www.gnu.org/software/bash/manual/html_node/Programmable-Completion-Builtins.html) |
  | zsh | `eval "$(zlib-cli completion zsh)"` in `~/.zshrc`, after `compinit` | `compdef` (https://zsh.sourceforge.io/Doc/Release/Completion-System.html) |
  | fish | `zlib-cli completion fish > ~/.config/fish/completions/zlib-cli.fish` | `complete -c` (https://fishshell.com/docs/current/cmds/complete.html) |
  | PowerShell | `zlib-cli completion powershell \| Out-String \| Invoke-Expression` in `$PROFILE` | `Register-ArgumentCompleter -Native -CommandName zlib-cli` (https://learn.microsoft.com/powershell/module/microsoft.powershell.core/register-argumentcompleter) |

- [ ] **`__complete`.** The glue calls a hidden command, `zlib-cli __complete SHELL -- WORDS...`.
  - It prints one candidate per line: commands, actions, options and choices (formats, orders, views,
    shells, config keys and their values).
  - Options that take a path fall back to the shell's file completion.
- [ ] **Offline and side-effect free.** Completion never opens a connection. It never reads credentials,
  the session, the token or the config file, and never writes anything. Tests patch the network and file
  access to prove it. Version 1 completes neither booklist IDs nor task IDs.
- [ ] **One source.** Completion is built from D3's command table rather than from argparse internals, and
  is tested on Python 3.11 and 3.14. It works for the installed `zlib-cli`, not for `python -m`.

### D7. HTTPS proxy

- [ ] **Settings.** The `proxy` key takes `http://HOST:PORT`: no credentials, no path. It comes with
  `--proxy URL`, `--no-proxy` and `ZLIB_PROXY`.
- [ ] **Transport.** HTTP CONNECT goes through `http.client.HTTPSConnection.set_tunnel`. TLS runs end to
  end to the site, and the certificate checks do not change.
- [ ] **Recorded decision.** Behind a proxy, the proxy resolves the site's name, so the public-address
  check moves to the proxy endpoint. A loopback or LAN proxy is accepted because it was configured
  explicitly.
- [ ] **Explicit only.** `HTTP_PROXY` and `HTTPS_PROXY` stay ignored. The local API client never uses the
  proxy. `doctor` reports whether a proxy was used.
- [ ] **Later.** SOCKS and Tor come later: the standard library has no SOCKS client.
- [ ] **Review.** A separate security review happens before the merge.

## 7. Command set after D1 to D6

The target list is `search`, `info`, `download`, `profile`, `quota`, `doctor`, `domains`, `library`,
`favorites`, `booklists`, `history`, `queue`, `similar`, `recommended`, `popular`, `recent`, `login`,
`logout`, `status`, `config`, `jobs`, `serve` and `tui`. D2 adds `config`, and D5 adds `queue` and
`recent`.

- The extras `formats`, `tags` and `shell` stay.
- `check` and `auth-test` become hidden legacy commands (D3).
- `completion` comes in D6, and `mcp` comes with its own design.
- No existing command is removed or renamed.

## 8. Not planned (recorded)

- Making `--with-pagination` the default for `search --json`. That would be a 1.0 change.
- A global `--yes`. `--yes` stays on each mutating action.
- Stdin for single-reference reads such as `info -`. The value is low; the same helper can add it later.
- `serve --read-only`. It matches the MCP rule "read-only by default" but is outside the CLI's scope. It is
  recorded for the HTTP API.

## 9. Shared contract with the HTTP API and MCP

- **One error vocabulary.**
  - The HTTP API returns `{"error": {"code", "message", "retryable", "outcome"}}`
    (`http_api/contract.py:13-25`).
  - The CLI error object carries the same fields, flattened, plus `error`, `exit_code` and the optional
    `reason`, `quota_may_be_used` and `hint`.
  - MCP tool errors carry `{code, message, retryable, outcome}`.
  - All three take their code from `error_codes.classify()` (D1).
  - Existing HTTP codes: `invalid_argument`, `invalid_request`, `unauthorized`, `booklist_not_found`,
    `mutation_uncertain`, `upstream_authentication_required`, `upstream_challenge`,
    `upstream_rate_limited`, `upstream_timeout`, `upstream_network_error`, `upstream_protocol_changed`,
    `upstream_refused`, `upstream_error`, `internal_error`, `client_busy`, `server_busy`, plus the task
    codes `download_not_found`, `download_conflict`, `download_not_ready`, `download_file_unavailable`,
    `download_capacity`, `downloads_busy`, `download_storage_error`, `downloads_unavailable` and
    `download_error`.
- **One set of shapes.** New CLI objects reuse the HTTP and model shapes (`BookPage`, `BookDetails`, task
  objects), so MCP tools can return them unchanged. MCP wraps array results in an object, because MCP
  requires an object root for `structuredContent`.
- **One settings resolver.** The MCP server calls `load_settings()` (D2) for the domain, timeout and
  `download_dir`. Downloads stay an explicit opt-in at launch with a fixed folder.
- **The same prompt rule.** The MCP server never prompts: stdin carries JSON-RPC. This is what `--no-input`
  means. A missing session is reported as `upstream_authentication_required` with the hint
  `Run zlib-cli login in a terminal first.`
- **Protocol facts** (for context; the MCP design owns them):
  - The current MCP revision is 2026-07-28. It is stateless: there is no `initialize` and requests carry
    `_meta`.
  - Today's clients still speak the legacy `initialize` handshake (2025-11-25 and 2025-06-18).
  - On stdio, stdout carries only newline-delimited JSON-RPC 2.0 messages in UTF-8; logs go to stderr.
  - Sources: https://modelcontextprotocol.io/specification/versioning and
    https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
- **Registration and environment.** The proposed launch command is `zlib-cli mcp`. Clients may pass only
  an allowlist of environment variables. The TypeScript SDK v1 stdio transport, for example, forwards:
  - on Windows: `APPDATA`, `HOMEDRIVE`, `HOMEPATH`, `LOCALAPPDATA`, `PATH`, `PROCESSOR_ARCHITECTURE`,
    `SYSTEMDRIVE`, `SYSTEMROOT`, `TEMP`, `USERNAME`, `USERPROFILE`, `PROGRAMFILES`;
  - on POSIX: `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`.

  The default config path of D2 resolves from `APPDATA` or `HOME`, so it works without extra environment.
  `ZLIB_CONFIG`, `ZLIB_STATE_DIR`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and the other `ZLIB_*` variables
  must be passed explicitly:
  - **Claude Code** (https://code.claude.com/docs/en/mcp):
    `claude mcp add --transport stdio --scope user zlib -- zlib-cli mcp`. With a variable:
    `claude mcp add --env ZLIB_CONFIG=C:\path\to\config.toml --transport stdio --scope user zlib -- zlib-cli mcp`.
    Keep another option between `--env` and the server name.
  - **Codex CLI** (https://learn.chatgpt.com/docs/extend/mcp?surface=cli):
    `codex mcp add zlib --env ZLIB_CONFIG=/path/to/config.toml -- zlib-cli mcp`. Or in
    `~/.codex/config.toml`: `[mcp_servers.zlib]` with `command = "zlib-cli"`, `args = ["mcp"]` and an
    `[mcp_servers.zlib.env]` table.
  - **Claude Desktop** (https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers): in
    `claude_desktop_config.json` (on Windows, `%APPDATA%\Claude\claude_desktop_config.json`):
    `{"mcpServers": {"zlib": {"command": "C:\\path\\to\\zlib-cli.exe", "args": ["mcp"], "env": {"ZLIB_CONFIG": "C:\\path\\to\\config.toml"}}}}`.
  - TypeScript SDK allowlist source:
    https://github.com/modelcontextprotocol/typescript-sdk/blob/v1.x/src/client/stdio.ts

## 10. Owner decisions

On 2026-11-12 the owner accepted the recommended answer to every question below. Questions 1, 2 and 7 apply to D1
and are implemented. Questions 3, 4, 5 and 9 apply to D2 and are implemented. Questions 6 and 8 belong to later
deliverables; only the decision is recorded here.

| # | Question | Recommended answer | Decision (2026-11-12) |
|---|---|---|---|
| 1 | Change the exit codes of single commands now (1 becomes 2, 3, 4 or 5), while the version is 0.1.0? | **Yes**, in D1 with the migration note. Scripts that test `!= 0` are unaffected, and agents get usable codes | Accepted. Done in D1, with the migration table in `docs/cli-json.md` |
| 2 | Turn prompts off automatically when stdin or stderr is not a terminal? Piped answers then need `--interactive` | **Yes.** This is the rule batch already uses, and it removes the hangs and the `< NUL` trap on Windows | Accepted. Done in D1; `--interactive` forces prompts on |
| 3 | Put `config.toml` in `%APPDATA%` (roaming), not `%LOCALAPPDATA%` beside the session? | **Yes, `%APPDATA%`.** Settings are preferences; the session and tokens stay local. A roamed `download_dir` that is missing on another PC fails with a clear message | Accepted. Done in D2: `%APPDATA%\zlibrary-api\config.toml`, and a missing configured folder gives `config_invalid` naming its source |
| 4 | Should a configured `domain` win over the saved session's domain, failing on a mismatch like `--domain` does today? | **Yes**, with the warning at `config set domain` and a mismatch message that names the source. Otherwise the setting would silently do nothing while a session exists | Accepted. Done in D2: the mismatch message names `--domain`, `ZLIB_DOMAIN`, `the config file (domain)` or `the credentials file (domain)`, and `config set domain` warns |
| 5 | Print a one-line notice when the legacy `credentials.local.toml` beside the package is used? | **Yes**, once per run. Keep the legacy path supported with no removal date: a plain-text password in site-packages deserves the nudge | Accepted. Done in D2: once per run on stderr, also in JSON mode; the legacy path stays supported |
| 6 | `recent`: on 2026-11-09, `/eapi/book/recently` returned only a success marker. Check it again before D5? | **Yes.** Make one read-only check on a domain you choose. If it still returns no list, drop `recent` from D5 and document `search QUERY --order date` | Accepted, before D5 |
| 7 | Ship D1 as one PR (output contract and input policy)? | **Yes**. They share the codes and the terminal test. Section 4.12 describes a fallback split | Accepted. D1 ships as one PR |
| 8 | Hide and deprecate `check` and `auth-test` in D3? | **Yes.** Their behavior and JSON stay. Remove them no earlier than one release after the notice | Accepted, for D3 |
| 9 | Add a per-run way to clear configured filters, such as `--format any`? | **Not in D2.** `--no-config` and explicit flags cover it. Revisit after real use | Accepted. Not in D2: `--no-config` and explicit `--format` or `--language` replace configured filters |

## 11. Appendices

### 11.1 Example lines that must keep parsing (fake values)

All 38 lines parse with today's `cli_parser.parser().parse_known_args()`, with no error and no leftover
argument. D1's parser test keeps them, and D3 seeds its examples from them.

```
search "dune" --format epub --language english --limit 10
search "9780441013593" --anonymous --json --with-pagination
info 1234567:a1b2c3 --json
formats 1234567:a1b2c3
download "dune frank herbert" --format epub --pick 1 --output .
download 1234567:a1b2c3 --output . --json
download -a reading-list.txt --dry-run
download -a reading-list.txt --pick 1 --yes
profile --json
quota
--timeout 5 doctor --json
doctor https://z-lib.by/ https://z-lib.sk/ --workers 2
domains --json
library --limit 3
favorites --order title --limit 50
favorites add 1234567:a1b2c3 --yes
favorites remove 1234567:a1b2c3
history --page 2 --json
booklists
booklists show 98765 --page 2
booklists add 98765 1234567:a1b2c3
booklists remove 98765 4242
booklists create "To read" --description "Next month" --private
booklists edit 98765 --title "Read soon" --public
booklists delete 98765
tags add 1234567:a1b2c3 "science fiction"
tags saved-ids --json
similar 1234567:a1b2c3 --limit 5
popular --anonymous --json
recommended --limit 10
--domain https://z-lib.by/ login
logout --json
status --verify --json
jobs add 1234567:a1b2c3 --key reading-2026-11-11-01
jobs list --limit 20
jobs watch 0123456789abcdef0123456789abcdef --duration 300 --json
serve --download-dir .
tui --view booklists
```

### 11.2 References

- Python `argparse`: https://docs.python.org/3/library/argparse.html
- `tomllib`: https://docs.python.org/3/library/tomllib.html
- `importlib.metadata`: https://docs.python.org/3/library/importlib.metadata.html
- Python UTF-8 mode on Windows: https://docs.python.org/3/using/windows.html#utf-8-mode
- TOML 1.0: https://toml.io/en/v1.0.0
- XDG Base Directory spec: https://specifications.freedesktop.org/basedir-spec/latest/
- NO_COLOR: https://no-color.org/
- Shell completion: the links in D6. MCP registration: the links in section 9.
