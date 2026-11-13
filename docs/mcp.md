# MCP server

`zlib-cli mcp` lets an AI assistant (Claude Code, Claude Desktop, OpenAI Codex or any other MCP client) search
Z-Library, read your account collections and, only if you enable it, download books or change your account. It
speaks the Model Context Protocol over stdin and stdout: the assistant starts it as a local command, and nothing
listens on a port.

Without flags, the server is **read-only**. Downloads and account changes each need their own flag, and the
assistant's client asks you to approve each such call.

## Start command and flags

```
zlib-cli [--domain ORIGIN] [--timeout SECONDS] [--session-file FILE] mcp
         [--anonymous] [--download-dir DIR] [--no-download-confirmation] [--allow-account-changes]
         [--print-config {claude-code,claude-desktop,codex}]
```

`python -m zlib_connector mcp` is the same command. Global options work before or after `mcp`.

| Flag | Effect |
|---|---|
| (none) | Read-only tools (13). Uses the session saved by `zlib-cli login`; without one, catalog tools work anonymously and account tools ask you to log in. |
| `--anonymous` | Never reads or sends your session. Only the 6 public read tools are listed. |
| `--download-dir DIR` | Adds the 5 download tools. `DIR` must be an existing folder (not a link) on a drive with hard links (NTFS and common Linux file systems; not FAT32 or exFAT). It is the only place the server writes files. |
| `--no-download-confirmation` | Needs `--download-dir`. Claude Code no longer asks you to confirm each `download_book` and `resume_downloads` call. Account changes still ask. Use it only if you approve downloads in bulk. |
| `--allow-account-changes` | Adds the 9 tools that change favorites, tags and booklists. |
| `--print-config CLIENT` | Prints a registration snippet for `claude-code`, `claude-desktop` or `codex` and exits, without serving. |
| `--timeout SECONDS` | Deadline of every request, from 0 to 300 seconds (default: the `timeout` setting, else 30). It also applies to a **whole book transfer**: use `--timeout 120` or more with downloads. |
| `--domain`, `--session-file`, `--config`, `--no-config` | As for the other commands. The domain (`--domain`, `ZLIB_DOMAIN` or the `domain` setting) must match the saved session's domain. A saved `download_dir` never enables the download tools. |

The server never asks for a password and never reads `credentials.local.toml`; sign in once with
`zlib-cli login` in a terminal. It refuses to start, with one line on stderr and exit code 1, when given
`--credentials` or `--no-session`, `--anonymous` with an action flag or `--session-file`,
`--no-download-confirmation` without `--download-dir`, a timeout outside (0, 300], an unusable download folder,
a `--domain` or configured domain that differs from the saved session, or an invalid setting
([Settings](reference.md#settings)). A missing or expired session does not stop it:
`server_status` reports it.

## Register it

`zlib-cli` must be on the client's `PATH`; otherwise use the absolute path of `zlib-cli.exe` (Windows) or
`zlib-cli` in your virtual environment. The easiest way is `--print-config`, which writes a ready-made entry that
uses the current Python (`python -m zlib_connector`) and the flags you give:

```sh
zlib-cli --timeout 120 mcp --download-dir /path/to/Books --print-config claude-code
```

**State directory.** Clients pass only a few environment variables to the server. If you set `ZLIB_STATE_DIR`
(or `XDG_STATE_HOME` on Linux), pass it through the client's `env` setting, or use `--session-file`; otherwise
the server does not find the session saved by `zlib-cli login` and `server_status` shows the session state
`empty`. The same holds for `ZLIB_CONFIG`, `XDG_CONFIG_HOME`, `ZLIB_DOMAIN`, `ZLIB_TIMEOUT` and `ZLIB_PROXY`; the default
settings file needs only `APPDATA` (Windows) or `HOME`, which clients pass. `--print-config` copies these
variables when they are set.

### Claude Code

```sh
# read-only, for all your projects
claude mcp add --transport stdio --scope user zlib -- zlib-cli mcp

# downloads into one folder, with a longer deadline for large files
claude mcp add --transport stdio --scope user zlib -- zlib-cli --timeout 120 mcp --download-dir /path/to/Books

# Windows, virtual environment not on PATH
claude mcp add --transport stdio --scope user zlib -- C:\path\to\.venv\Scripts\zlib-cli.exe --timeout 120 mcp --download-dir D:\Books

# custom state directory: keep another option between --env and the server name
claude mcp add --env ZLIB_STATE_DIR=C:\path\to\state --transport stdio --scope user zlib -- zlib-cli mcp

# JSON form (also the shape of a .mcp.json entry with --scope project)
claude mcp add-json zlib '{"type":"stdio","command":"zlib-cli","args":["--timeout","120","mcp","--download-dir","/path/to/Books","--allow-account-changes"]}' --scope user

claude mcp get zlib    # status; /mcp inside a session
```

- Everything after `--` reaches the server unchanged.
- Scopes: the default is `local`; `--scope project` writes `.mcp.json`; `--scope user` writes `~/.claude.json`.
- Downloads and account changes carry `anthropic/requiresUserInteraction`: Claude Code asks you on **every**
  call, even in auto or bypass mode, with no "don't ask again". `--no-download-confirmation` removes that prompt
  from `download_book` and `resume_downloads` only.
- Claude Code's default runtime speaks 2026-07-28 with this server; `MCP_PROTOCOL_NEGOTIATION=legacy` makes it use
  `initialize` instead. Both work.

### Claude Desktop

Edit `claude_desktop_config.json` (Settings > Developer > Edit Config). It lives in
`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on macOS and `~/.config/Claude/` on Linux.

```json
{
  "mcpServers": {
    "zlib": {
      "command": "C:\\path\\to\\.venv\\Scripts\\zlib-cli.exe",
      "args": ["--timeout", "120", "mcp", "--download-dir", "D:\\Books"],
      "env": { "ZLIB_STATE_DIR": "C:\\path\\to\\state" }
    }
  }
}
```

Quit Claude Desktop completely and restart it. Drop `env` when you do not need it. Logs are in
`%APPDATA%\Claude\logs\mcp*.log`, `~/Library/Logs/Claude` or `~/.config/Claude/logs`. Whether Claude Desktop
honours tool annotations or the confirmation key is not documented, so keep actions off unless you need them.

### OpenAI Codex

```sh
codex mcp add zlib -- zlib-cli mcp
codex mcp add zlib --env ZLIB_STATE_DIR=/path/to/state -- zlib-cli mcp
codex mcp list
```

For downloads or account changes, edit `~/.codex/config.toml` (shared by the Codex CLI, the IDE extension and the
ChatGPT desktop app; a project `.codex/config.toml` works for trusted projects only):

```toml
[mcp_servers.zlib]
command = 'C:\path\to\.venv\Scripts\zlib-cli.exe'   # TOML literal string: no backslash escapes
args = ["--timeout", "120", "mcp", "--download-dir", 'D:\Books']
startup_timeout_sec = 20          # default 10
tool_timeout_sec = 180            # default 60; keep it at least --timeout + 15
default_tools_approval_mode = "writes"   # prompts for every tool not marked read-only

[mcp_servers.zlib.tools.download_book]
approval_mode = "prompt"
[mcp_servers.zlib.tools.resume_downloads]
approval_mode = "prompt"
# with --allow-account-changes, add the same block for each account-change tool:
# add_favorite, remove_favorite, add_tags, remove_tags, create_booklist, update_booklist,
# delete_booklist, add_to_booklist, remove_from_booklist

[mcp_servers.zlib.env]
# ZLIB_STATE_DIR = 'C:\path\to\state'
```

With `--no-download-confirmation`, `--print-config codex` leaves out the `download_book` and `resume_downloads`
blocks, but `default_tools_approval_mode = "writes"` still asks for every tool that is not read-only; relax it
only if you accept downloads without approval. Other keys: `env_vars = ["NAME"]` forwards variables from the
parent, plus `cwd`, `enabled`, `enabled_tools` and `disabled_tools`.

## Tools

Read tools (`--anonymous` lists only the first six):

| Tool | What it does |
|---|---|
| `server_status` | Version, domain, sign-in state, download folder and enabled actions; no network request. |
| `search_books` | Search with `query`, `page`, `limit` (1 to 20), `languages`, `formats`, `year_from`, `year_to`, `order`. |
| `get_book` | Details of one edition: description, publisher, ISBNs, categories, site page URL. |
| `list_formats` | Other files of the same book, each with its own reference. |
| `list_similar`, `list_popular` | Site suggestions, at most `limit`. |
| `list_recommended` | Recommendations for your account. |
| `get_quota` | Daily download quota: limit, used, remaining (reading it uses no quota). |
| `list_favorites`, `list_history` | One page of favorites, or of the site's download history. |
| `list_booklists`, `list_booklist_books` | Your booklists, and one page (20 entries) of a list with each `entry_id`. |
| `list_tags` | Your My Library tag names. |

Download tools (`--download-dir`):

| Tool | What it does |
|---|---|
| `download_book` | **Uses quota.** Queues one exact edition (`reference` = `ID:HASH`) and returns its task. Asks for approval. |
| `get_download` | Task state, progress and, once done, the file's name and absolute path. `wait_seconds` (up to 30) waits for the end. |
| `list_downloads` | Tasks in the folder, newest first, and whether the queue is paused for quota. |
| `cancel_download` | Cancels a queued or running task. |
| `resume_downloads` | **Uses quota.** Restarts tasks paused because the quota ran out. Asks for approval. |

Account-change tools (`--allow-account-changes`; each one asks for approval):

| Tool | What it does |
|---|---|
| `add_favorite`, `remove_favorite` | Save or remove an edition in your favorites. |
| `add_tags`, `remove_tags` | Add or remove My Library tags; adding tags also saves the book to favorites. |
| `create_booklist` | Creates a list, private unless `is_public` is true. |
| `update_booklist` | Changes only the settings given. |
| `delete_booklist` | **Permanent.** Needs `expected_title`, the list's current title, or nothing is deleted. |
| `add_to_booklist`, `remove_from_booklist` | Add an edition to a list, or remove an entry by its `entry_id`. |

Every result is JSON, in `structuredContent` and as the same text. A failed call returns `isError: true` with
`{"error": {"code", "message", "retryable", "outcome"}}`, as in the [HTTP API](http-api.md#errors-and-limits).

## Safety model

- **Off by default.** Only read tools exist until you pass `--download-dir` or `--allow-account-changes`.
- **Approval prompts.** Claude Code asks on every download, quota resume and account change; Codex asks with the
  settings above. Tool annotations mark read and action tools honestly.
- **Exact edition.** `download_book` takes one `ID:HASH` reference, never a search query. Each search or format
  result is one edition; the assistant is told to show the options and let you choose.
- **Quota.** Before queuing, the server checks the format (PDF, EPUB, DjVu) and, when the account is free, the
  quota, so a refused book uses nothing. Repeating `download_book` with the same reference returns the same task
  and never downloads twice, even after a restart.
- **No automatic retry.** A failed, cancelled or interrupted download may already have used quota, so it is never
  retried. The assistant can try again only by passing `retry_task_id`, which you approve. Account changes are
  never retried either: an unconfirmed change returns `mutation_uncertain` with outcome `unknown`.
- **Untrusted metadata.** Titles, descriptions, tags and list names come from third parties. They are returned as
  plain text with terminal escapes and control characters removed, never as instructions or links.
- **No secrets.** Cookies, session tokens, account IDs and download links never appear in results or logs, and
  tool arguments are never logged.

## Files and lifetime

Books are saved as `DIR/<task_id>/<Title>-<12 hex>.<ext>`; the task journal lives in `DIR/.zlib-jobs/`, shared
with the [download tasks](download-tasks.md) of `zlib-cli serve`. One process at a time can own a folder for an
account: a running `zlib-cli serve --download-dir` on the same folder makes the download tools return
`downloads_busy`.

Downloads live as long as the MCP server, which your client starts and stops. Closing the client (or restarting
Claude Desktop) interrupts the current transfer: that task ends `interrupted`, with an unknown quota outcome, and
is not retried. Queued tasks wait and start the next time a download tool is used.

## Limits

| Limit | Value |
|---|---|
| Page size | 1 to 20 (default 10); `page` 1 to 1000 |
| Similar, popular, recommended, formats | at most 20 entries; `available` gives the total |
| Booklists, tags, categories | 100 lists, 200 tag names, 20 categories |
| Text | titles and authors 300 characters, descriptions 2,000 (`description_truncated`), other fields 100; a cut value ends with `...` |
| Result size | 24 KiB; items are dropped from the end and `truncated: true` is set |
| Download tasks per page | 1 to 50 (default 20) |
| Concurrent calls | 4; more get `server_busy` |
| `wait_seconds` | at most 30 |

## Protocol notes

- Revision `2026-07-28` (stateless: `server/discover`, a version in every request's `_meta`, `resultType` on every
  result), and the legacy revisions `2025-11-25` and `2025-06-18` through `initialize`. The client's first request
  picks one for the whole process: `initialize` on a 2026-07-28 connection gets `-32022`, and a 2026-07-28 request
  on a legacy connection gets `-32600`.
- On 2026-07-28, `ping` and `logging/setLevel` are gone (`-32601`). `subscriptions/listen` is acknowledged with an
  empty filter, since the tool list never changes, and stays open until it is cancelled.
- Only tools are declared: no resources, prompts, sampling or MCP logging. Logs go to stderr only.
- `notifications/cancelled` stops waits and lock waits; a request that was cancelled gets no response. Cancelling
  `get_download` stops only the wait, never the download; use `cancel_download`. A change already sent to
  Z-Library may still apply.
- `get_download` with `wait_seconds` and a `progressToken` sends progress notifications, at most two per second.
- The tool list is fixed for the life of the process and depends only on the flags.

## Troubleshooting

| What you see | What to try |
|---|---|
| `upstream_authentication_required` | Run `zlib-cli login` in a terminal; the next call uses the new session. After `zlib-cli logout`, restart the client to stop using the old one. |
| `server_status` says `empty` although you logged in | Pass `ZLIB_STATE_DIR` (or `XDG_STATE_HOME`) through the client's `env`, or use `--session-file`. |
| `rejected` | The saved session was refused, or belongs to another domain than `--domain`: log in again on the right domain. |
| `client_busy` | A download holds the account connection; searches still work. Try again shortly. |
| `downloads_busy` | Another `zlib-cli serve` or `mcp` uses this folder for the same account. Stop it or choose another folder. |
| Transfers end `interrupted` or `failed` with large files | Raise `--timeout` (up to 300) and, in Codex, `tool_timeout_sec`. |
| The server is not found | Use the absolute path of `zlib-cli.exe` or `python -m zlib_connector`; on Windows, write TOML paths as literal strings (`'C:\...'`). |
| Claude Desktop shows nothing | Restart it completely and read `mcp*.log` in its log folder. |
| Codex times out | Raise `startup_timeout_sec` and `tool_timeout_sec` (default 60). |
