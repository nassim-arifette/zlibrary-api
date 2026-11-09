# Zlibrary API

Find a book on Z-Library, choose the edition you want, and download it from your
terminal or Python.

The project is built around a reading workflow: find books, keep track of what
you want to read, and bring them into the reader or application you use. The goal
is to make your Z-Library account and collections available through a CLI, a TUI
and an HTTP API, with the same behavior across them.

**Available today:** a Python library, CLI and interactive shell, including
browsing favorites, download history and your booklists. Editing collections,
the TUI and the HTTP API are planned. This is an unofficial client.

## Get started

You need **Python 3.11 or later**. From the project folder, install the command:

```sh
python -m pip install -e .
```

You can also run it without installing: replace `zlib-cli` in the examples with
`python -m zlib_connector`.

### Find a working domain

```sh
zlib-cli --timeout 5 doctor
```

`doctor` tries a real search on the known domains and suggests one that responds.
It reports blocked requests, connection problems and timeouts without signing in.
The result depends on your network and when you run it.

The examples below use `https://z-lib.by/`. Replace it with a working domain from
your results. Options such as `--domain`, `--timeout` and `--credentials` go
**before** the command name.

### Search for a book

```sh
zlib-cli --domain https://z-lib.by/ search "Pride and Prejudice" --anonymous --format epub --limit 5
```

Search by title, author or ISBN. `--anonymous` lets you browse without loading
your account details. It uses the domain you pass explicitly, or `z-lib.by` by
default.

To narrow the results, choose a language, publication years and a sort order:

```sh
zlib-cli --domain https://z-lib.by/ search "history" --anonymous --language french --year-from 2000 --order bestmatch
```

You can repeat `--language` and `--format` to include several values. Use
`--page 2` for the next page, or add `--json --with-pagination` to include the
available page counts and navigation in a script.

### Choose and download

```sh
zlib-cli --domain https://z-lib.by/ download "Pride and Prejudice" --format epub --output .
```

The command signs in, lists the results and asks which one to download. If you
haven't saved your credentials, it prompts for your email and password. Password
input is hidden. Downloads support **PDF, EPUB and DjVu** and require an existing
destination folder; `.` means the current folder.

For a precise edition, use the `book_id` and `book_hash` from a JSON search result
as `ID:HASH`. Replace that placeholder in these commands with the actual values:

```sh
zlib-cli --domain https://z-lib.by/ info "ID:HASH" --anonymous
zlib-cli --domain https://z-lib.by/ formats "ID:HASH" --anonymous
zlib-cli --domain https://z-lib.by/ download "ID:HASH" --output .
```

The details include the description, publisher, edition, pages and valid ISBNs
when the service supplies them. Alternative formats have their own references.
A direct download uses the edition you selected without running another search.
You can also pass its book URL from the selected domain.

### Check your quota

```sh
zlib-cli --domain https://z-lib.by/ quota
```

This shows your daily limit, downloads used and downloads remaining. If a counter
is missing from the response, it appears as `unknown`. Add `--json` for scripts.

## Browse your library

```sh
zlib-cli library
zlib-cli favorites --limit 10
zlib-cli history --page 2
zlib-cli booklists
```

`library` gives you an overview with separate previews of your favorites,
account download history and booklists. The history comes from Z-Library; it
doesn't mean those files are still on this computer.

To open one of your booklists, use the ID shown next to its name:

```sh
zlib-cli booklists show BOOKLIST_ID --page 1
```

Replace `BOOKLIST_ID` with that number. You can inspect or download any listed
book using its `ID:HASH` reference. Add `--json` to any of these commands for
scripts. Reading a list does not modify its books or visibility.

## Keep a session open

Use the interactive shell to search and download several books after one login:

```sh
zlib-cli --domain https://z-lib.by/ shell
```

```text
zlib> search "history" --language french --format epub
zlib> next
zlib> prev
zlib> download 1 .
zlib> quit
```

Result numbers refer to the most recently displayed page. The session ends when
you leave the shell. Separate CLI commands currently sign in again each time.

## Save your sign-in details locally

To avoid entering them for each command, create `credentials.local.toml` in the
project folder, next to `pyproject.toml`, or edit it if it already exists:

```toml
domain = "https://z-lib.by/"
email = "you@example.com"
password = ""
```

Fill in your account details locally. Leave the password empty if you prefer to
enter it at the prompt. This file stores any saved password as plain text, so
keep it private; Git ignores it. Credentials and session keys stay out of command
output. Session cookies are currently kept only in memory.

Use `--credentials PATH` before the command to select another file. `--domain`
overrides its configured domain. Anonymous commands and domain diagnostics never
load the credentials file.

## Use it from Python or a script

```python
from zlib_connector import Client, SearchFilters

filters = SearchFilters(languages=("english",), formats=("epub",))

with Client("https://z-lib.by/") as client:
    page = client.search_page("Pride and Prejudice", filters=filters, limit=5)
    for book in page.books:
        print(f"{book.book_id}:{book.book_hash}  {book.title}")
```

The Python library also exposes book details, available formats, downloads,
account quota and domain diagnostics. See the [reference](docs/reference.md) for
login examples, return types and errors.

For shell scripts, add `--json` to `search`, `info`, `formats`, `download`,
`profile`, `quota`, `doctor`, `domains`, `library`, `favorites`, `history` or
`booklists`. Search normally returns an array;
`--with-pagination` adds a `books` and `pagination` object. Results go to standard
output, while prompts and errors go to standard error.

## If something goes wrong

| What you see | What to try |
| --- | --- |
| A domain is blocked or stops responding | Run `doctor` again and explicitly select a working domain. |
| Login fails | Check the domain and credentials, then run `auth-test`. Repeated login attempts won't resolve a domain block. |
| A browser verification challenge | Try another domain reported by `doctor`. This client cannot complete browser challenges. |
| No downloads remaining | Check `quota` and wait for the account allowance to renew. |
| A file cannot be saved | Choose an existing writable folder and check available disk space. |
| Details or formats are missing | Some records have incomplete metadata or no reported alternatives. |

Use `zlib-cli --help` or `zlib-cli COMMAND --help` for the available options.
For more context on diagnostics and errors, see the [reference](docs/reference.md).

## Where the project is going

The next steps focus on your library and reading lists:

- **Favorites:** add and remove saved books, and see their saved status while
  browsing.
- **Booklists:** create and edit lists, and add or remove books where the service
  supports those operations. Browsing existing lists is available now.
- **History and discovery:** track local files alongside account history and
  discover similar books, personal recommendations and popular titles.
- **Reading queues:** import a list, choose editions, skip duplicates and follow
  downloads with progress, cancellation and resume where supported.
- **Persistent sessions:** reuse a login between commands.
- **TUI and HTTP API:** browse in a full terminal interface or connect another
  app through a documented API. Both will use the same core as the Python client
  and CLI.
- **Uploads:** send a book with its metadata and track its processing, once the
  service's upload flow and destination visibility have been verified.

These are planned features. The current interactive shell is available now; the
full TUI and HTTP server are not implemented yet.

## Development

The package has no runtime dependencies. Run the tests from the project folder:

```sh
python -m unittest discover -s tests -q
```

The [technical reference](docs/reference.md) covers protocol behavior, file
validation and recorded compatibility checks. Z-Library's endpoints and domains
can change, so a successful check is specific to its date and network.
