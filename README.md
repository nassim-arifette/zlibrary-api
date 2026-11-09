# Zlibrary API

Standalone Python API and CLI for Z-Library. Python 3.11+, no runtime package
dependencies, no desktop application or web server required.

## Start

From this directory:

```powershell
python -m zlib_connector check
python -m zlib_connector auth-test
python -m zlib_connector shell
```

The configured default is `https://z-lib.by/`, verified with a real account on
2026-11-09. `1lib.sk` and `z-lib.sk` returned DiamWall HTTP 513 in the same session.
`--domain HTTPS_ORIGIN` overrides the selected domain. Credentials are never
automatically forwarded to another origin or tried against a list of mirrors.

## Credentials

Authenticated commands read `credentials.local.toml` beside this README. This
file is ignored by Git. Restrict its permissions to your own account.
It contains a local plaintext password; do not share or commit it.

```toml
domain = "https://z-lib.by/"
email = "your-account@example.com"
password = ""
```

To create the local file, run `Copy-Item credentials.example.toml credentials.local.toml`
only if it does not already exist. Fill in the email and password locally. Missing email/password values are prompted. For
passwords containing backslashes or double quotes, TOML single-quoted literal
strings can be used unless the password itself contains a single quote.
`--credentials PATH` selects another TOML file. Neither credentials nor session
cookies are printed in errors, logs or JSON results. Session cookies remain in
memory and are discarded when the client closes.

## Commands

```powershell
# Authenticated search; JSON output is suitable for a future application adapter.
python -m zlib_connector search "title or author" --format epub --json

# Download a selected result into an existing directory.
python -m zlib_connector download "title" --pick 1 --output "C:\Books"

# Public search without loading the credentials file.
python -m zlib_connector search "Pride and Prejudice" --anonymous --limit 3

# Check an explicitly selected domain, without credentials.
python -m zlib_connector --domain https://z-lib.by/ check
```

Global flags (`--domain`, `--credentials`, `--timeout`) go before the command.
Repeat `--format` and `--language` to select several formats/languages. Search and
query-based download also accept `--year-from`, `--year-to` and `--order`:

```powershell
python -m zlib_connector search "history" --anonymous --language french --language english --format pdf --format epub --year-from 2000 --year-to 2026 --order bestmatch
python -m zlib_connector search "history" --anonymous --json --with-pagination
```

Orders are `bestmatch`, `popular`, `date` (recently added), `titleA` (A–Z),
`title` (Z–A), `year`, `filesize` (largest first), and `filesizeA` (smallest first).
These are upstream orders; the client does not re-sort only one page. Actual filter
and ordering behavior can vary by domain. Exact and full-text search are not exposed.
`--json` retains its original array shape; `--with-pagination` opts into an object
containing `books` and `pagination`. Missing totals/navigation are `null`, never
guessed from the number of returned books. Totals describe the upstream result set.

`check` exercises a real public API search, not merely the site's homepage or
domain listing. `auth-test` validates both login cookies and the account profile.

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
use stderr. Exit status is 0 on success, 1 on failure, 130 on interruption.
Optionally install the console entry point with `python -m pip install -e .` and
use `zlib-cli` with the same arguments. Installation is not needed for `python -m`.

## Python API

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
mutually exclusive. Page bounds remain 1–1000 and page size 1–50.

Clients are synchronous and not thread-safe; use a separate client per session.

Error types are `AuthenticationError`, `ChallengeRequired`, `RateLimitError`,
`FormatChangedError`, `NetworkError` and `DownloadError`, all derived from
`ConnectorError`. Invalid local arguments raise `ValueError`.

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
  deadline. Download data is buffered in memory.
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
results matching the requested English language, PDF/EPUB formats and 2000–2026
year range, with pagination metadata. The selected `titleA` order was accepted;
this check does not establish every ordering rule on every mirror. The live API's
`false` navigation sentinel is covered by a synthetic regression test.
