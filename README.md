# Zlibrary API

An API, CLI and TUI for Z-Library.

Search for books, inspect editions, download files and access your favorites,
booklists and download history.

**Available today:** a Python library, CLI and interactive shell, including
browsing and updating favorites, reading download history, and creating, editing
and deleting booklists. The TUI and the HTTP API are planned.
This is an unofficial client.

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

To save a book for later or remove it from your favorites:

```sh
zlib-cli favorites add "ID:HASH"
zlib-cli favorites remove "ID:HASH"
```

Replace `ID:HASH` with the book's reference. The CLI asks before changing the
account. Scripts must pass `--yes` explicitly and can add `--json` for the result.
Saved books carry a `[saved]` marker when their state is known. If a connection
problem leaves an update unconfirmed, check your favorites before trying again.

To add a book to an existing booklist, or remove an entry from it:

```sh
zlib-cli booklists add BOOKLIST_ID "ID:HASH"
zlib-cli booklists remove BOOKLIST_ID ENTRY_ID
```

`booklists show` displays the **Booklist entry** number needed for removal; JSON
calls it `entry_id`. This number identifies the entry in that list, so it differs
from the book ID. Both commands ask for confirmation. Use `--yes --json` in a
script. If an update cannot be confirmed, reopen the list before retrying.

To create and manage your lists:

```sh
zlib-cli booklists create "Science" --description "Books to explore"
zlib-cli booklists edit BOOKLIST_ID --title "Science and nature"
zlib-cli booklists edit BOOKLIST_ID --public
zlib-cli booklists delete BOOKLIST_ID
```

New lists are **private by default**. Choose `--public` to share one, or `--private`
to make it private. Editing changes only the settings you provide; for example,
renaming keeps the current description, visibility and color. Descriptions can
contain up to 1000 characters and names up to 120. `--color` accepts 0 through 5.

Deletion is permanent on Z-Library and asks for confirmation. As with other
account changes, scripts must pass `--yes`; add `--json` for the returned result.

## Find your next book

```sh
zlib-cli popular --anonymous --limit 10
zlib-cli similar "ID:HASH" --anonymous
zlib-cli recommended
```

`popular` shows the service's popular titles, `similar` starts from a book's
reference, and `recommended` uses your account's recommendations. Add `--json`
to use the results in a script. `--limit` controls how many books are shown from
the returned list; these views do not currently expose pagination or filters.

Some suggestions include only a title, author and reference. Open their details
with `info` or pass the reference to `download` to resolve the available format.
The recent-additions endpoint currently returns no book list on the tested
domain, so that view is not available yet.

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
`booklists`, as well as `similar`, `popular` and `recommended`. Search normally returns an array;
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

- **Booklists:** move selections between lists, import/export their contents and
  browse public lists. Creating, editing, deleting and adding or removing books
  are available.
- **Local history:** find downloaded files on your computer and detect missing
  files or duplicates alongside the account history.
- **Reading queues:** import a list, choose editions, skip duplicates and follow
  downloads with progress, cancellation and resume where supported.
- **Persistent sessions:** reuse a login between commands.
- **TUI and HTTP API:** use Z-Library from an interactive terminal or through
  documented HTTP endpoints. Both will use the same core as the Python client
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

GitHub Actions runs the offline tests on Windows and Linux with Python 3.11 and
3.14. It also installs the package and checks the installed CLI outside the source
folder. These checks do not use a Z-Library account or contact its servers.

The [technical reference](docs/reference.md) covers protocol behavior, file
validation and recorded compatibility checks. Z-Library's endpoints and domains
can change, so a successful check is specific to its date and network.
