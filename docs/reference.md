# Technical reference

For installation and everyday use, start with the [user guide](../README.md).
This page describes the current Python and CLI contracts and their limitations.

## CLI behavior

Global flags (`--domain`, `--credentials`, `--timeout`) go before the command.
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
