# Technical reference

For installation and everyday use, start with the [user guide](../README.md).
This page describes the current Python and CLI contracts and their limitations.
For the local HTTP server, see the [HTTP API guide](http-api.md).
The optional [TUI](tui.md) uses this server through a bounded local
catalog client; ordinary Python and CLI use does not require Textual.

## CLI behavior

Global flags (`--domain`, `--credentials`, `--timeout`, `--session-file`,
`--no-session`) go before the command.
Repeat `--format` and `--language` to select several formats/languages. Search and
query-based download also accept `--year-from`, `--year-to` and `--order`:

```powershell
python -m zlib_connector search "history" --anonymous --language french --language english --format pdf --format epub --year-from 2000 --year-to 2026 --order bestmatch
python -m zlib_connector search "history" --anonymous --json --with-pagination
```

Orders are `bestmatch`, `popular`, `date` (recently added), `titleA` (A to Z),
`title` (Z to A), `year`, `filesize` (largest first), and `filesizeA` (smallest first).
These are upstream orders; the client does not re-sort only one page. Actual filter
and ordering behavior can vary by domain. Exact and full-text search are not exposed.
`--json` retains its original array shape; `--with-pagination` opts into an object
containing `books` and `pagination`. Missing totals/navigation are `null`, never
guessed from the number of returned books. Totals describe the upstream result set.

`check` exercises a real public API search, not merely the site's homepage or
domain listing. `auth-test` validates both login cookies and the account profile.

`doctor` probes the known candidate domains with isolated anonymous clients and
up to four concurrent requests (`--workers 1` through `8`). Supply explicit HTTPS
origins after `doctor` to limit the list, or use global `--domain` to probe one.
Global `--timeout` applies per request. `domains [--json]` lists candidates without
network access; their presence does not imply current availability.

```powershell
python -m zlib_connector --timeout 5 doctor https://z-lib.by/ https://z-lib.sk/ --json
```

The report includes measured elapsed time, UTC check time, real-search success,
and typed failures (DNS, TLS/certificate, timeout, challenge, redirect, rate limit,
authentication requirement or incompatible response). A successful probe confirms
only anonymous search from this network at that time. The fastest successful
candidate is suggested; configuration and credentials are never changed or sent.
No success returns exit status 1; at least one success returns 0.

Python callers can use `diagnose_domains([...], timeout=5, workers=4)` to obtain
a `DomainReport`, its `recommended_domain` and `to_dict()` result. A successful
probe's final origin matches the tested origin; failed redirects are reported,
not silently followed with form data. Probes do not authenticate or download files.

Interactive mode logs in once and keeps the session until exit:

```text
zlib> search "title or author"
zlib> search "history" --language french --order year
zlib> next
zlib> prev
zlib> download 1 "C:/Books"
zlib> quit
```

`--json` returns successful search/download results on stdout; prompts and failures
use stderr. Exit status is 0 on success, 1 on failure, 2 for a command-line usage
error and 130 on interruption. Batch downloads add 3 and 4 (see
[Batch downloads](#batch-downloads)).
Optionally install the console entry point with `python -m pip install -e .` and
use `zlib-cli` with the same arguments. Installation is not needed for `python -m`.

## Python API

### Persistent sessions

```python
from zlib_connector import Client, SessionStore

store = SessionStore()  # Default local application state directory.
with Client("https://z-lib.by/") as client:
    client.login(email, password, session_store=store)

with Client("https://z-lib.by/") as client:
    status = client.restore_session(store)  # Confirms the account remotely.
    history = client.get_history()

print(store.read().status())  # Safe local metadata; no cookies or account IDs.
store.clear()  # Forget local cookies; does not revoke a remote session.
```

`login(..., session_store=store)` persists only after the domain confirms the
session. Without that argument, login remains in memory. `restore_session()`
requires the same canonical HTTPS origin and unexpired authentication cookies;
it confirms the account before returning `verified: true`. Errors clear the
client's imported cookies but leave the stored file unchanged. A network failure
is not treated as expiry, and no automatic password login follows a failed
restore. `close()` never writes the store or saves rotated cookies.

The store contains only the selected origin, save time, authentication cookies
and their path/expiry restrictions, plus a format version and generation. There
is no password, email or raw profile. Restored cookies are secure and limited to
the exact host. Browser cookie import and cross-domain session migration are not
implemented. Low-level session objects contain secrets and must not be logged
or serialized into an application response; use `snapshot.status()` instead.

The default path is `state_directory()/session.json`; `SessionStore(path)` uses
an explicit file in an existing parent directory. A missing store can be read
without creating files. Writes use a private temporary file, fsync and atomic
replacement under a bounded interprocess lock. Existing unsafe permissions,
links, malformed or oversized files are rejected rather than repaired silently.
Cookie JSON is protected by Windows ACLs or POSIX permissions, not encryption.
`SessionStore(..., lock_timeout=5)` allows up to five seconds to acquire the
writer lock and, on Windows, up to five more if an open reader temporarily blocks
replacement. A failed publication preserves the previous file and removes its
own temporary file. These waits never repeat a network authentication request.

Each write changes a generation. A login captures that generation before the
network request and saves only if it is unchanged. `clear()` retains an empty
record with a new generation, so a login already in flight cannot recreate the
forgotten session. The stable lock file is not removed. A running HTTP server
or shell keeps its own in-memory session after local logout.

`SessionError`, `SessionConflictError` and `SessionLockTimeoutError` are
`ConnectorError` subclasses. A conflicting login or logout fails explicitly;
the caller decides whether to repeat it. No network request runs under the file
lock. Clients remain synchronous and are not safe for simultaneous calls.

CLI commands `login`, `logout`, and `status [--verify]` accept `--json`. Local
status reports `empty`, `stored` or `expired`, with `verified: false`; stored
cookies alone do not prove remote validity. Verification requires a usable
saved session and adds `authenticated: true` on success. The CLI keeps its normal
exit codes: 0 for successful local inspection, 1 for a failed verification or
operation. `--no-session` cannot be combined with these session-management
commands. A supplied `--domain` must match before `status` or `logout` can act on
an existing session.

Domain selection for account commands is: explicit `--domain`, the domain in an
explicit `--credentials` file, saved session origin, then the default credentials
domain. Explicit `login` still reads credentials for fresh authentication.
Ordinary commands reuse the saved session; if none exists, they retain their
previous in-memory login behavior. `auth-test` ignores persistent sessions.
Anonymous commands and domain diagnostics do not access session or credential
files. The HTTP launcher shares this selection logic and binds its listener
before any upstream session confirmation; it exposes no HTTP login endpoint.

### Search and download

```python
from zlib_connector import Client, ConnectorError
from zlib_connector.credentials import load_credentials

credentials = load_credentials()
try:
    with Client(credentials.domain, timeout=45) as client:
        client.login(credentials.email, credentials.password)
        books = client.search("title or author", extension="epub", page=1, limit=20)
        if books:
            saved_path = client.download(books[0], "C:/Books")
            print(saved_path)
except ConnectorError as error:
    print(type(error).__name__, str(error))
```

`Client.search()` also works without login where the service allows public search.
`download()` requires an authenticated session and a `Book` returned by that domain.
Book records preserve the API's numeric ID and hash independently of website URLs.
Use `SearchFilters` for the richer interface while keeping existing `search()` calls:

```python
from zlib_connector import Client, SearchFilters

filters = SearchFilters(languages=("french",), formats=("pdf", "epub"),
                        year_from=2000, order="bestmatch")
with Client() as client:
    page = client.search_page("history", filters=filters, limit=10)
    print(page.to_dict())
    # Lazy traversal follows the server's next page, stopping at page 1000.
    for book in client.iter_search("history", filters=filters, limit=10):
        print(book.title)
```

`search()` still returns a list. `search_page()` returns an immutable `SearchPage`;
`iter_search()` requests each next page only as it is consumed and stops if the
server provides no next page. The legacy `extension=` and `filters.formats` are
mutually exclusive. Page bounds remain 1 to 1000 and page size 1 to 50.

`get_book("ID:HASH")` returns a `BookDetails` record with description, cover URL,
publisher, edition, pages, categories and identifiers when supplied by the server.
ISBN-10/13 fields are populated only after checksum validation. Descriptions are
plain text; missing optional metadata remains empty. `get_formats("ID:HASH")`
returns `BookFormat` records with each alternative's own ID/hash. An empty list is
a valid upstream answer; formats beyond PDF/EPUB/DjVu are marked unsupported for
download. These lookups do not download covers or create account state.

`get_book()`, `get_formats()` and `download()` also accept a book HTTPS URL on the
selected domain (`/book/ID/HASH[/title]`, without query parameters or a fragment).
`download()` continues to accept a `Book`. Direct-reference CLI downloads reject
search filters and `--pick`, ensuring the selected edition cannot silently change.
`info` and `formats` use the configured account unless `--anonymous` is provided.

`get_profile()` returns an `AccountProfile` with a safe quota projection;
`get_quota()` returns a `DownloadQuota` directly. Each call fetches fresh account
data and requires a logged-in client. The current public profile contains only
the quota, with no raw account dictionary, email, password or session keys.
The CLI equivalents are `profile [--json]` and `quota [--json]`.
Quota fields are `limit`, `used` and `remaining`; remaining is clamped to zero
when usage exceeds the current limit. Missing counters remain `null` (or `unknown`
in human-readable output), while malformed counters raise `FormatChangedError`.

Clients are synchronous and not thread-safe; use a separate client per session.

Error types are `AuthenticationError`, `ChallengeRequired`, `RateLimitError`,
`FormatChangedError`, `NetworkError` and `DownloadError`, all derived from
`ConnectorError`. Invalid local arguments raise `ValueError`.
An edit targeting a list absent from the account's editable collection raises
`BooklistNotFoundError`, a `ValueError` subclass, before any write is attempted.

## Streaming downloads and cancellation

`Client.download(book_or_reference, destination, progress=None, cancel=None)`
still returns the published `Path`. Progress and cancellation are keyword-only.
The client opens a temporary file before requesting the download link, streams
the response in chunks of at most 64 KiB, then validates the file and publishes
it atomically without overwriting an existing destination. The same file module
serves the JSON and legacy HTML clients. No local download history is created.

```python
from zlib_connector import CancellationToken, TransferCancelled

token = CancellationToken()

def report(update):
    print(update.to_dict())
    # token.cancel() may also be called by a controlling thread or UI action.

try:
    path = client.download("ID:HASH", "downloads", progress=report, cancel=token)
except TransferCancelled:
    print("Download cancelled")
```

`TransferProgress` is an immutable snapshot with `bytes_received`, `total_bytes`
and `elapsed_seconds`. Its derived `bytes_per_second`, `eta_seconds` and
`fraction` remain `None` when they cannot be calculated. The clock starts at
the file transfer; metadata lookup is not part of its elapsed time. A missing
Content-Length is reported as an unknown total. Progress contains no file URL,
session information or local path. Receiving 100% of the bytes does not imply
that validation and publication have succeeded; only the returned path does.

Callbacks run synchronously in the downloading thread. A callback exception
aborts the transfer and removes the temporary file; it is not converted to a
network error. No callback runs after publication. The built-in CLI renderer
disables itself if stderr is closed or fails, preserving the download result.

Cancellation is cooperative: the token is checked between requests, received
chunks and publication steps. Once a socket is connected, a short-lived guard
interrupts it on cancellation or expiry of the absolute deadline, including
internal header and chunk-framing reads. DNS and connection setup may still
have to return or reach their normal timeout first. Keep progress callbacks
short; the client does not interrupt user callback code. Ctrl+C also unwinds the temporary file and
closes the connection. The CLI returns 130 for interruption. No operation is
automatically repeated, because obtaining a link may already consume quota.
Interrupted partial files are removed; byte-range resume remains planned.
For persistent tasks, progress polling and file retrieval through Python, HTTP
or the CLI, see [download tasks](download-tasks.md).

`DownloadError.reason` names its cause without parsing messages: `unavailable`,
`no_link` and `refused` (the service declined the link request), `format_mismatch`,
`protocol`, `size_limit`, `invalid_book`, `local_write`, `save_failed`, or the
generic `download`. Messages and classes are unchanged. An exception raised by
`Client.download()` after it sent the quota-using link request carries
`download_stage`: `link` while that request was pending, `transfer` once a link was
issued (transfer, validation and publication). Its absence means no link was
requested, so no quota was used.

`Transport.download_to(url, binary_stream, progress=None, cancel=None)` is the
lower-level streaming operation and returns the number of received bytes. It
does not own, validate or publish the destination stream. It shares the HTTPS,
cookie, redirect and deadline implementation with `request()`. Content-Length
is checked against the configured limit and received body according to HTTP
framing. These checks and file signatures do not establish the upstream book's
content integrity against an independent checksum.

PDF and DjVu validation checks their file signatures. EPUB validation reads its
directory before constructing a ZIP reader, with an 8 MiB directory and 10,000
entry limit. Only the `mimetype` marker is decompressed, capped at 100 bytes
uncompressed and 1 KiB compressed using STORE or DEFLATE. Ordinary ZIP64 local
headers work when the central sizes are bounded; ZIP64 end records and
multi-volume archives are currently rejected. Book payloads are never read
entirely into memory for this validation. Other EPUB resources are not unpacked
or checked for full semantic correctness.

## Batch downloads

`download` keeps its single-book flow, prompts and `{"path": ...}` JSON for exactly
one query without list options. Several queries, `-a/--batch-file FILE` (`-` reads
standard input) or any list option selects batch mode: `--batch-format`,
`--dry-run`, `--report FILE`, `--no-report`, `--yes`, `--interactive`,
`--retry-uncertain`, `--redownload-missing` or `--max-downloads N`. Titles may
follow options; titles and `-a` cannot be combined. In batch mode `--limit` is the
number of candidates per search (default 5, at most 50), `--page` is refused and
`--pick` must not exceed `--limit`. Every option, the list, the output folder and
an existing report are checked before a request is sent, and before credentials
are read except with `-a -`. A missing `--yes` is refused before anything is
read. `-a -` checks for a saved session or an email in the credentials file
before it reads standard input, so a refusal does not consume the list. A list
that is the credentials file itself (the default one or `--credentials FILE`) is
refused without being read.

A run is **interactive** when stdin and stderr are terminals (or `--interactive`
is given) and the list is not read from standard input; on Windows a terminal is
a console, so a `NUL` redirect does not count as one. Candidates, the plan
summary, the confirmation and progress use stderr, so stdout can be redirected.
The prompt `Choose 1-N, s to skip, q to stop:` asks again after an invalid answer;
end of input counts as `q`. `q` stops choosing: items that need no choice are
still resolved, then the confirmation `Download N books into FOLDER? [y/N]`
states the quota (`Download N of M books` when `--max-downloads` is lower). A
non-interactive download requires `--yes`, and `--pick N`
when an item needs a search result choice; `-a -` also requires a saved session
or an email in the credentials file, because the list occupies standard input.

### List formats

Lists are UTF-8 with or without a byte-order mark, or UTF-16 with one (Windows
PowerShell 5.1 redirection writes it). Line ends may be LF, CRLF or CR. A list
holds at most 16 MiB and 1000 items. `--batch-format` selects the format, else
the suffix `.txt`, `.csv`, `.tsv` or `.json`; otherwise JSON is assumed when the
text starts with `[` or `{`, and text otherwise. Text whose first line is a CSV
header, or a single column name such as `Title`, `ISBN` or `Author`, is refused
unless `--batch-format txt` is given, and so is text that holds JSON, such as a
plan saved as `plan.txt`: its first line is a lone `[` or `{`, or the whole text
parses as JSON. Name it `.json` or use `--batch-format json`; titles such as
`[Graphic novel] Maus` stay searches. Invalid encoding, NUL characters, a CSV
header without a usable column, a CSV quote that is never closed, a text line
that sets `email` or `password` (a credentials file), JSON syntax, JSON nested
deeper than 32 levels and the size limits reject the whole list; any other problem
turns that one entry into an
`error` that names its line or item.

| Input | Items |
| --- | --- |
| Text | One per line. Blank lines and lines starting with `#` are skipped, ignoring invisible characters such as a byte-order mark; put a title starting with `#` in a CSV or JSON list. |
| CSV | A header row, then one item per row. Delimiter: tab for `.tsv`; otherwise comma, semicolon or tab, found in the header, and a header without any of them is one column. A row with more cells than the header, or a used value that spans several rows, is an item error: quote values that contain the delimiter. |
| JSON | An array, or an object with an `items` or `books` array, of strings or objects. |
| Plan or report | A document written by this command; see below. |

A value with `://`, or digits and a colon without spaces, must be an `ID:HASH`
reference or an http(s) `/book/ID/HASH` URL from any mirror. Only its ID and hash
are kept and used with the selected domain; an invalid one is an error, never a
search. Everything else is a search: whitespace is collapsed, text is NFC
normalized, invisible format characters other than zero-width (non-)joiners are
removed, control characters make the item an error, and 1 to 500 characters are
allowed. `2001: A Space Odyssey` is a search; `123:ab12/` is an invalid reference.

CSV headers and JSON keys are case-insensitive: `query`; `title` (a trailing
series marker such as `(The Expanse, #1)` is removed); `author` or `authors`;
`isbn`, `isbn13`, `isbn10` or `isbn/uid` (`="..."` spreadsheet wrappers are
removed, separators dropped and checksums required; a number in scientific
notation is an error); `reference`, `url`, or `book_id` with `book_hash`;
`extension`; `format`, used only when its values are pdf, epub or djvu; and
`language`. Several values can be separated by `,`, `;` or `|`. A reference wins
over search fields, and a `url` counts only when it is a book URL. The search is
`query`, else `title` plus `author`, else the first valid ISBN. Empty cells are
absent and blank rows skipped. Other columns are ignored and listed once on
stderr; the PowerShell `#TYPE` line is skipped. Per-row `extension`/`format` and
`language` replace the batch filters field by field; year and order filters are
inherited. Filters apply only to searches; a reference outside an explicit
`--format` is downloaded as is, with a note in the plan.

### Planning

An item's identity is its reference, or its query (NFC, case-folded, whitespace
collapsed) with its effective filters. A repeated identity is a `duplicate` of
the first item without another search; a second item resolving to an edition
already chosen is one too (`duplicate_of` is the first item's index). References
are confirmed with `get_book()`; formats other than PDF, EPUB and DjVu are errors.
Searches read page 1. With `--pick N`, an item with fewer than N results is
`not_found`. Without a terminal and without `--pick`, `--dry-run` records
`needs_choice` items with their candidates. Each lookup starts at least 0.5
seconds after the previous one ended, as politeness rather than a retry; time
spent at a prompt counts toward the pause.

Authentication, browser-verification, rate-limit and network errors stop
planning; two protocol errors in a row do too, while a single one is an item error.
The rest is then resolved only from the report without requests, and nothing is
downloaded. The choices made so far are saved in the report, unless the run is a
dry run or the confirmation is declined; declining leaves an existing report
byte for byte unchanged.

### Downloading

The output folder must accept a hard link and a file named like the longest book
file (108 characters), so FAT32 and exFAT drives, and on Windows folder paths too
long for book names, fail before any request. On Linux and macOS, where names are
limited in bytes (usually 255), a long title in a script such as Chinese or
Japanese is shortened by whole characters until the name fits; the single-book
download names its files the same way. The report is written before the first
download. For each planned book, the
quota is read first: a confirmed zero stops with the rest `waiting_quota`; missing
counters are unknown and do not block; a failed read stops the batch with nothing
requested. An edition taken from a plan or report is confirmed with `get_book()`.
The report then marks the item `downloading` before `Client.download()` streams,
validates and publishes the file as usual. The quota is refreshed at the end,
except after a cancellation or a refused session: no request is made then, and
the report's `quota` is null when a download was requested since the last read,
so no stale count is shown.

| Failure | This item | Rest |
| --- | --- | --- |
| Quota read failed, or the edition check could not be made (network, session refused, verification, rate limit) | `not_started` (`waiting_quota` for a rate limit) | the same; nothing is requested |
| Edition check rejected the edition (not PDF, EPUB or DjVu, not valid for this domain, unexpected response or API refusal) | `error`, no quota used; checked again next run | continue; see below |
| Link refused: rate limit | `waiting_quota` | `waiting_quota` |
| Link refused: unavailable or no link | `error` if the quota is still above zero, else `waiting_quota` | continue, or `waiting_quota` |
| Network, verification or protocol error after the link request | `error`, quota may be used | `not_started` |
| Local save failure (disk, permissions, hard links) | `error`, quota may be used; `not_started` when it failed before the link request | `not_started` |
| Invalid file, or a format other than the listed one (`FormatMismatch`) | `error`, quota may be used | continue; see below |
| File over the 256 MiB limit | `error` `TooLarge`, quota may be used; never requested again, even with `--retry-uncertain` | continue |
| First Ctrl+C, or a cancelled `stop` token | finishes and is recorded | `not_started` |
| Second Ctrl+C, or a cancelled `cancel` token | `cancelled`, quota may be used once the link was requested | `not_started` |
| Report write failure | stops before the next request | `not_started` |

A rejected edition, an invalid file and a format mismatch count as failures: a
second one before another book is downloaded stops the batch with the rest
`not_started`. An edition that is not PDF, EPUB or DjVu, or a file over the size
limit, never stops it.

`quota_may_be_used` is true when the link request may have been honored: the
item is never retried automatically, in this run or a later one, unless
`--retry-uncertain` (`retry_uncertain=True`) is given. That option releases the
attempts recorded before the run. A new uncertain failure in such a run says that
the option would request it again, and the advice is to run the command again
without it. A released item that the run does not reach stays uncertain. A
`TooLarge` edition is never released: the same file would fail again, so it stays
an error, and any item that resolves to that edition is one too, without a
request. Running the list again keeps it; to get another edition, replace the
item with that edition's `ID:HASH`, or reword its search, for example by adding
the author. Either changes the item's identity, while a change of case or spacing
alone does not. In the
CLI, the first Ctrl+C lets the current book finish and be recorded, then stops
(a press before its link request uses no quota); a second one cancels that
download at its next check, after a pending request or chunk; a third one stops
at once. `--max-downloads N` stops after N attempts with the rest `not_started`.

### Reports

With `-a FILE`, the report defaults to `FILE` without its suffix plus
`.progress.json`, beside the list; a report given with `-a` is its own report.
A plan given with `-a`, including `-a -`, has no default report: downloading it
needs `--report FILE` or `--no-report`. Pass the list's report, which the dry run
names on stderr, so that later runs of the list know what the plan downloaded. A
report piped with `-a -` needs them too; give it with `-a FILE` to continue it.
`--report FILE` chooses another file in an existing folder and `--no-report`
disables it. Titles and standard input have no default report. Without a report
and without `--no-report`, a non-interactive run stops before downloading when
the known quota cannot cover the planned books (or is unknown), or when
`--max-downloads` is lower than their number, because a later run would download
the finished books again; an interactive confirmation warns. When a run without a
report stops early after downloading or attempting a book, its last lines name
the items still waiting, to be run again on their own. A run
holds `REPORT.lock` beside the report for its whole duration; a second run fails
immediately. The lock file stays empty and needs no private permissions, so
copied, synced and shared folders work; where files cannot be locked, choose a
`--report` file in a local folder or add `--no-report`. Dry runs only read the
report, take no lock, and say that nothing was written. Each write creates a temporary file in
the same folder, fsyncs it, then replaces the report atomically, waiting up to two
seconds on a Windows sharing violation. The JSON is UTF-8 with ASCII escapes. Book
files are stored by bare file name, with the output folder in `folder`. Reports
contain no tokens, cookies, signed links or credentials, and messages come from a
fixed table.

A report is read strictly (32 MiB and 10,000 entries at most, duplicate keys,
nesting deeper than 32 levels, unknown fields and invalid values refused); an
invalid report, a plan or any other
JSON is refused and never overwritten. On the next run, each item is matched by
identity before any search or prompt:

- recorded downloads whose files exist become `already_downloaded` with no request;
  missing files, and downloads recorded with a null `path`, stay
  `already_downloaded` unless `--redownload-missing`;
- a recorded file is looked up by its name in the current output folder only
  (`--output`, or `folder=` in Python), never in the document's `folder` or as
  written; a `path` that is not a plain file name (with a folder, drive, UNC or
  device part) is never opened and counts as missing;
- when the report's `folder` is another folder and none of its recorded books is
  in the output folder, a non-interactive download stops before any request and
  names that folder (pass it with `--output`); a dry run or a terminal run says so
  on stderr and goes on, and the advice for the missing files is that folder, not
  `--redownload-missing`. The report keeps its `folder` until a book is found or
  downloaded in the new one;
- skips stay skipped; chosen editions are reused without searching or asking;
- `downloading` entries become `interrupted`; they and other attempts that may have
  used quota are kept, unless `--retry-uncertain`;
- `not_found` items, errors before any link request and new lines are resolved again.

Entries matching no current item move to `previous` and still count as known
downloads. The document is `{kind, version: 1, created_at, updated_at, folder,
quota, stopped, summary, items, previous}`, where `kind` is
`zlib-connector-batch-report`, `quota` is `{limit, used, remaining}` or null,
`stopped` is `{error_code, message}` or null and `summary` counts non-zero statuses.
A `--dry-run --json` plan has the same shape with `kind:
zlib-connector-batch-plan` and no `previous`. Each item is:

```json
{"index": 1, "position": "line 3",
 "source": {"query": "dune herbert", "filters": {"formats": ["epub"], "languages": [], "year_from": null, "year_to": null, "order": null}},
 "status": "downloaded", "reference": "123:ab12",
 "book": {"reference": "123:ab12", "title": "Dune", "authors": "Frank Herbert", "extension": "epub", "year": "1965", "language": "english", "size": "1 MB"},
 "path": "Dune-0123456789ab.epub", "duplicate_of": null, "error_code": null, "message": null,
 "quota_may_be_used": false, "candidates": []}
```

`source` is `{"reference": "ID:HASH"}` for a reference and null for an invalid
entry. `reference` is the chosen edition; `book` describes the edition that
`status`, `path` and `quota_may_be_used` refer to, and `path` is a bare file name
inside `folder` without control characters. Reading a plan or report back as a
list uses `reference` or `pick` (1-based, into `candidates`), else `source`;
`book` is display-only and never searched. To change a choice, edit `reference`
or add `pick`; to search again, set `reference` to null. This works only for
items that were not downloaded or attempted: once an item is `downloaded`,
`already_downloaded`, `downloading`, `interrupted`, `error` `TooLarge`, or
`error` or `cancelled` with `quota_may_be_used`, a `reference` that differs from
its `book`, or a `pick`, makes the whole document invalid. To get another edition
too, add its `ID:HASH` as a new line of the list, or a new item such as
`{"source": {"reference": "ID:HASH"}, "status": "planned"}`; a copy of the item
with another `reference` repeats the same search and is skipped as a duplicate.
Saving a plan over the file being read (`-a plan.json ... > plan.json`) empties it
before the command starts, and the empty file is refused.

| Status | Meaning | Task state |
| --- | --- | --- |
| `downloaded` | Published in this run. | `succeeded` |
| `already_downloaded` | Recorded earlier; no request. | |
| `duplicate` | Same request or edition as `duplicate_of`. | |
| `skipped` | Skipped by a choice; kept on later runs. | |
| `planned` | Edition chosen, not attempted yet. | `queued` |
| `needs_choice` | Plans only: choose among `candidates`. | |
| `not_found` | No result, or fewer than `--pick`. | |
| `error` | Input or download problem; see `error_code` and `quota_may_be_used`. | `failed` |
| `waiting_quota` | Not attempted: quota or rate limit. | `waiting_quota` |
| `not_started` | Not attempted: the batch stopped first. | `queued` |
| `cancelled` | Cancelled during this item. | `cancelled` |
| `interrupted` | An earlier attempt may have used quota (a run stopped during it, or it was not retried); kept until `--retry-uncertain`. | `interrupted` |
| `downloading` | Transient write-ahead state. | `running` |

Batch exit codes: 0 when every item is `downloaded`, `already_downloaded`,
`duplicate` or `skipped` (`planned` too for a dry run); 3 when the others only wait
(`waiting_quota`, `not_started`, `planned`) because of the quota, a rate limit,
`--max-downloads`, a network failure or `q`; 4 when an item is `not_found`, an
`error`, `needs_choice` or `interrupted`, when the batch stopped on an account,
verification, protocol, storage or report problem; 130 when cancelled or declined.
1 still means that the batch could not start, and 2 a usage error.

### Python API

```python
from pathlib import Path
from zlib_connector import (Client, SearchFilters, SessionStore, load_report, parse_batch, pick,
                            plan_batch, run_batch)

items = parse_batch(Path("reading-list.csv"))            # Path: a file; str: list text
report_path = Path("books/progress.json")
with Client("https://z-lib.by/") as client:
    client.restore_session(SessionStore())               # or client.login(email, password)
    plan = plan_batch(client, items, filters=SearchFilters(formats=("epub",)), choose=pick(1),
                      previous=load_report(report_path),  # None when the report does not exist
                      folder="books")                       # where recorded files are looked up
    report = run_batch(client, plan, "books", report=report_path)
print(report.summary, report.stopped)
```

`parse_batch(source, *, kind=None, warn=None)` takes an `os.PathLike` file, list
text, bytes or a sequence of strings and returns `BatchItem` values (`position`,
`query`, `reference`, `filters`, plus a plan's `choice` and the recorded outcome:
`recorded` with its `path`, `uncertain`, `too_large` or `skipped`).
`plan_batch(client, items,
*, filters, candidates=5, choose, previous, retry_uncertain, redownload_missing,
on_item, pause, folder)` returns a `BatchPlan`; `folder` is the output folder where
recorded file names are looked up (default: the current folder), and `pause` is
the minimum number of seconds between lookups. Give `run_batch()` the same folder:
it refuses, before any request, a plan that looked up recorded files in another
one. `choose(item, books)` returns one of `books`
or None to skip, and may raise `StopPlanning`; without `choose`, items become
`needs_choice`. Ctrl+C or `TransferCancelled` returns the partial plan with
`cancelled` set. `run_batch(client, plan, folder, *, report, progress_factory,
cancel, on_item, max_downloads, stop)` returns a `BatchReport`; `progress_factory(entry)`
returns a context manager yielding a progress callback, as `ProgressDisplay` does.
A cancelled `stop` token lets the current book finish and stops before the next
one; a cancelled `cancel` token also cancels the current transfer.
An existing report must be passed to `plan_batch(previous=load_report(path))`
first. `report_lock(path)` in `zlib_connector.batch` gives the CLI's exclusive lock.
Plans, reports and their `BatchEntry` items have `to_dict()`. The client is
synchronous and not thread-safe: `run_batch()` must own it, and must not share it
with a `DownloadManager` unless it holds that manager's client lock. HTTP and TUI
support for lists is planned.

## Account collections

Authenticated clients expose these read operations:

| Method | Result |
| --- | --- |
| `get_library(limit=5)` | `LibraryOverview` with separate favorites, history and booklists. |
| `get_favorites(page=1, limit=20, order="saved_date")` | A `BookPage` of saved books. |
| `get_history(page=1, limit=20, order=None)` | A `BookPage` of account downloads. |
| `get_booklists()` | The booklists returned for the account, with ID, title, count and visibility when present. |
| `get_booklist_books(booklist_id, page=1)` | A `BookPage`; the service controls its page size. |

`BookPage` holds the pagination contract shared by search and account collections.
`SearchPage` remains an alias for compatibility. Favorite orders are `saved_date`,
`saved_dateA`, `titleA`, `title`, `year`, `filesize` and `filesizeA`. History accepts
the corresponding `downloaded_date` and `downloaded_dateA` instead of save dates;
the server may ignore history ordering. The client preserves the returned order.

Collection reads preserve sparse book records and unsupported formats so books do
not silently disappear. `download()` fetches details if a book's format is absent;
you can also pass its reference directly. Missing collection arrays are protocol errors;
explicit empty arrays are valid. Counts and visibility stay unknown when absent.
Raw owner fields and booklist-entry annotations are not exposed.

The CLI commands are `library`, `favorites`, `history`, `booklists [list]` and
`booklists show BOOKLIST_ID`. All support `--json`. Favorites/history and booklist
contents return `{books, pagination}`; the booklist index returns an array.
These browsing operations read account data only. They do not inventory local
files or edit a list. The overview does not assume that a specific
booklist called "My Library" exists on the account.

Booklists currently use `GET /papi/booklist/current-user/0` and a read-only
`POST /papi/booklist/{id}/get-books/{page}` with an empty body. Their response
shapes were checked against the configured account on 2026-11-09. The
[KOReader implementation](https://github.com/ZlibraryKO/zlibrary.koplugin/blob/main/zlibrary/api.lua)
provided the endpoint reference. No account content or credentials were recorded
in verification output.

`set_favorite("ID:HASH", saved=True)` adds a favorite; `saved=False` removes it.
It requires login and returns `FavoriteUpdate(book_id, is_favorite)` after the
server reports success. The CLI exposes `favorites add` and `favorites remove`,
with an interactive confirmation or explicit `--yes` for scripts. Invalid input
and declined confirmations do not send an account update.

Although the upstream save/unsave endpoints use GET, the client treats them as
mutations: it does not follow redirects or retry them. A network failure or
unreadable response raises `MutationUncertainError`; query favorites before
retrying. These mutation paths are covered with synthetic responses, including
redirects and cancellation, rather than changes to the configured account.
The endpoint reference is the [existing Python wrapper](https://github.com/bipinkrish/Zlibrary-API/blob/main/Zlibrary.py).

### Native My Library tags

| Python method | CLI command | Result |
| --- | --- | --- |
| `get_library_tags()` | `tags [list]` | `LibraryTags`, serialized as `{tags: [...]}`. |
| `get_saved_book_ids()` | `tags saved-ids` | Tuple of IDs; CLI JSON is `{book_ids: [...]}`. |
| `add_book_tags(reference, tags)` | `tags add ID:HASH LABEL [LABEL ...]` | `BookTagUpdate`. Also adds the book to favorites. |
| `remove_book_tags(reference, tags)` | `tags remove ID:HASH LABEL [LABEL ...]` | `BookTagUpdate`. Keeps the favorite. |

All four require login. Mutations require a nonempty list or tuple of nonblank
labels. New labels are limited to 30 UTF-16 code units, matching the website's
input limit; removing an existing longer label is allowed. Control characters
are rejected. Exact duplicates are removed, but case,
spaces and Unicode spelling are preserved. The service enforces the combined
tag limit for the book. The catalog exposes names only; its opaque values are
not interpreted as counts or colors. An empty upstream `[]` catalog is valid.

`BookTagUpdate.to_dict()` contains `book_id`, `action`, `tags`, and
`adds_to_favorites`. It acknowledges the submitted change after server success;
it is not a snapshot of all tags on the book. Unreadable mutation responses raise
`MutationUncertainError`, with no redirect or automatic retry. The CLI confirms
before login, or requires `--yes` in scripts. Adding tags explicitly mentions
its effect on favorites.

The site's [bookmark script](https://z-lib.by/resources/js/user-bookmarks.js)
and [tag component](https://z-lib.by/components/z-tags.js), inspected on
2026-11-09, use `GET /papi/my-library/tags/`,
`GET /papi/my-library/saved-book-ids`, and `PUT`/`DELETE`
`/papi/my-library/tags/{book_id}` with a JSON `{tags: [...]}` body.
Account reads confirmed the empty-catalog and saved-ID response shapes.
Writes are covered by synthetic tests; no real tags were changed.

The native book view uses a server-rendered profile page with search, tag and
page filters. Its authenticated HTML could not be verified on the tested domain
because of a browser challenge. Full native browsing and per-book tag reads
remain unavailable; neither saved IDs nor the collection overview substitute
for that view.

### Books in booklists

`get_booklist_books()` returns `BooklistBook` records, which extend `Book` with
`booklist_id` and `entry_id`. The entry ID comes from `readlist_books_id` in the
server's wrapper around each book. It is distinct from the catalog book ID and
remains null when absent. Wrapped favorite flags are also preserved.

| Python method | CLI command |
| --- | --- |
| `add_booklist_book(booklist_id, "ID:HASH")` | `booklists add BOOKLIST_ID ID:HASH` |
| `remove_booklist_book(booklist_id, entry_id)` | `booklists remove BOOKLIST_ID ENTRY_ID` |

These require login and return `BooklistBookUpdate` with the list, action and
affected book or entry ID after a confirmed response. Adding a book does not
invent its new entry ID; read the list to obtain it. CLI updates require an
interactive confirmation or `--yes`. The client never follows redirects or
retries these mutations. An unreadable result raises `MutationUncertainError`.

The site's [bookmark script](https://z-lib.by/resources/js/user-bookmarks.js),
read on 2026-11-09, uses `GET /papi/booklist/{id}/add-book/{book_id}` and
`GET /papi/booklist/{id}/remove-book/{entry_id}`. Account reads confirmed the
entry schema; mutation behavior is covered by synthetic tests. No real booklist
was modified to validate these commands.

### Booklist settings

| Python method | CLI command |
| --- | --- |
| `create_booklist(title, description="", is_public=False, color=0)` | `booklists create TITLE [--description TEXT] [--public] [--color 0..5]` |
| `update_booklist(id, title=None, description=None, is_public=None, color=None)` | `booklists edit ID [--title TEXT] [--description TEXT] [--public / --private] [--color 0..5]` |
| `delete_booklist(id)` | `booklists delete ID` |

Settings are keyword-only after the title or ID. New lists are private unless
explicitly made public. The client validates a nonblank title of at most 120
characters, a description of at most 1000 characters, a boolean visibility and
an integer color from 0 to 5. Control characters are rejected except newlines
and tabs in descriptions. An empty description clears it; `None` preserves it.

Editing fetches the current account list and sends the site's full settings form,
preserving the exact description text and other unmodified values. Missing
settings prevent an update; a list absent from the account's editable collection
cannot be edited. The upstream protocol has no verified conditional-write
mechanism, so concurrent edits to the same list can overwrite each other.

Creation and editing return a `Booklist`, now including the optional `color`.
The response must confirm the expected settings (and the same ID for an edit);
an incomplete or conflicting result raises `MutationUncertainError`. Deletion
returns `BooklistDeletion`, serialized as `{booklist_id, deleted: true}` after
the service confirms success. All three CLI commands require confirmation or
`--yes`, and support `--json`. Deleting a list does not delete downloaded files.

The site's current [booklist component](https://z-lib.by/components/z-booklist.js),
[list browser](https://z-lib.by/components/z-booklists-browser.js) and
[request helper](https://z-lib.by/resources/build/main.js), inspected on
2026-11-09, establish `POST /papi/booklist/create`,
`POST /papi/booklist/{id}/edit` and `GET /papi/booklist/{id}/delete`.
The first two send a JSON body using the browser helper's
`text/plain;charset=UTF-8` content type. A private list includes `private: "on"`;
a public list omits that checkbox field. The client uses explicit settings
instead of the site's visibility-toggle endpoint.

The existing account settings were checked read-only. Creation, editing,
deletion, refusals and uncertain results are covered by synthetic tests;
no real lists were created, changed, published or deleted during verification.

Book records now include `is_favorite`: true/false when the service supplies a
recognized saved flag, otherwise null. Books read from the favorites collection
are marked true. A missing flag is not interpreted as an unsaved book.

## Discovery

`get_popular()` and `get_similar("ID:HASH")` can be used anonymously where the
domain permits it. `get_recommendations()` requires login. Each returns the list
supplied by the server, with validated references and optional metadata. The
client does not fetch every suggestion's details automatically. An explicit empty
list is valid; a response missing its books list raises `FormatChangedError`.

The CLI commands are `popular`, `similar ID:HASH` and `recommended`, with `--json`
and a display `--limit` from 1 to 1000 (default 20). Popular and similar also
accept `--anonymous`. The display limit trims the server's list locally; it is
not an upstream pagination or language-filter request.

These methods were checked in read-only calls on 2026-11-09. Public similar and
popular responses contain sparse records; personalized recommendations also
returned a books list. `/eapi/book/recently` returned a success marker without a
books list on that domain, so no recent-additions command is exposed yet.

## Protocol and safeguards

Login uses the website's `/rpc.php` endpoint, then verifies `/eapi/user/profile`.
Search uses `/eapi/book/search`; download links come from
`/eapi/book/{id}/{hash}/file`. These are unofficial mobile endpoints whose contracts
may change. References: [mobile API documentation](https://github.com/baroxyton/zlibrary-eapi-documentation)
and [observed response models](https://github.com/heartleo/zlib/blob/main/docs/android-eapi-deep-dive.md).

- HTTPS certificate verification, public DNS validation and pinned addresses on
  every hop. No system proxy configuration is used.
- Same-origin 307/308 redirects retain form data; cross-origin form replay is
  rejected. API authentication headers are derived only from cookies applicable
  to the destination and are not forwarded to download CDNs.
- HTML pages are not parsed as JSON or saved as books. PDF/EPUB/DjVu signatures
  are validated before publication; download permissions and formats are checked.
- Responses are bounded: 4 MiB for API/HTML, 256 MiB per book, with a request
  deadline. Client downloads stream to disk with a 64 KiB transfer buffer;
  metadata responses remain buffered. Direct legacy use of
  `Transport.request(download=True)` still returns a buffered response.
- Files are written to a temporary file, then atomically published with a random
  filename suffix without overwriting existing files. The destination must exist
  and support hard links (NTFS does).
- Remote metadata is stripped of terminal control characters. Raw server error
  bodies, credentials and session tokens are never shown.

## Verification

```powershell
python -m unittest discover -s tests -v
```

The tests use synthetic accounts and temporary directories. A real, user-authorized
end-to-end run on 2026-11-09 successfully logged in on `z-lib.by`, confirmed the
account through the JSON API, found three EPUB results and downloaded a valid
407,595-byte EPUB of Jane Austen's *Pride & Prejudice* into `downloads/`.
No credentials or session URLs were recorded in the verification output.

On 2026-11-09, an anonymous advanced-search check on `z-lib.by` returned three
results matching the requested English language, PDF/EPUB formats and 2000 to 2026
year range, with pagination metadata. The selected `titleA` order was accepted;
this check does not establish every ordering rule on every mirror. The live API's
`false` navigation sentinel is covered by a synthetic regression test.

The same day's anonymous check retrieved a matching book-details record and 17
alternative-format records. Some detail fields were absent and remained empty.
Direct-reference downloading is covered by synthetic tests through the existing
validated file-download flow; that check did not consume an account download.

An authorized account-profile check on 2026-11-09 confirmed populated quota
counters and consistent remaining-download calculation. Verification output
contained only success flags; no credentials or raw profile fields were recorded.

An anonymous `doctor` run on 2026-11-09 confirmed public search on `z-lib.by` and
classified `z-lib.sk` as a browser challenge. These observations are time- and
network-specific; rerun diagnostics before choosing a domain.
