# Terminal interface

Browse your favorites, personal booklists and Z-Library download history, search
the catalog with filters, and discover popular, recommended and similar books in
an interactive terminal. Open a book for its description and metadata, then
return to the same row. The TUI does not change your account. It downloads a book
only when you press `d` and confirm. Collection editing is planned.

## Start

From the project folder:

```sh
python -m pip install -e ".[tui]"
```

Then start it:

```sh
zlib-cli tui
```

The TUI talks to the local HTTP API. If a `zlib-cli serve` already answers at
the configured address, it uses it. Otherwise it starts a private server inside
the same process for this session, signed in with your saved session (or after
asking for your email and password in the terminal), and stops it when you
quit. That private server listens on a random loopback port and is not shared
with other programs; run `zlib-cli serve` yourself if you want one server for
the TUI, scripts and the MCP server. `--no-auto-server` turns the private
server off.

To use a server started with a different address or token file, pass the same
values; the TUI then never starts its own:

```sh
zlib-cli tui --api-url http://127.0.0.1:8123/v1 --token-file ./api-token
zlib-cli tui --view search --page-size 30
zlib-cli --timeout 10 tui --no-color
```

The TUI opens on Home, which reads nothing from the server. `--view` can also be
`favorites`, `booklists`, `history`, `search`, `popular` or `recommended`;
`search` opens the search form. Page size is 1–50 for favorites, history and
search results; the service controls booklist page size. The global `--timeout`
goes before `tui`. `--no-color` and the `NO_COLOR` environment variable turn
colors off.

## Home and commands

Home shows the title and the commands that open each section: `/search`,
`/popular`, `/recommended`, `/favorites`, `/booklists` and `/history`.
Terminals at least 76 columns wide show them in two columns. Clicking a command
also opens it. The line at the bottom lists the main keys.

Press `/` (or Ctrl+P) on any page to open the command dialog. Type to filter the
list, for example `pop` for `/popular`; ↑/↓ choose and Enter runs the
highlighted command. Keys typed straight after `/`, however fast, go to the
dialog, and Enter waits until the list matches what you typed. Esc closes the
dialog and returns to the page and row you were on. While the dialog or the
theme list is open, the bottom line shows its keys; the help shows its keys
under its text.

| Command | Action |
| --- | --- |
| `/search` | Search books |
| `/favorites` | Saved books |
| `/booklists` | Your booklists |
| `/history` | Download history |
| `/popular` | Popular books |
| `/recommended` | Recommended for you |
| `/similar` | More like this; listed only when a book is selected or open |
| `/download` | Download the selected or open book; listed only when downloads are available |
| `/refresh` | Reload this page; listed on every page except Home and the search form |
| `/home` | Home |
| `/help` | Keys and tips |
| `/theme` | Change the color theme until you quit |
| `/quit` | Quit |

## Keys

| Key | Action |
| --- | --- |
| `/` or Ctrl+P | Command dialog |
| `s` or `4` | Search: the form, or the current results when you come from another section |
| `f` or `1` | Favorites |
| `b` or `2` | Booklists |
| `h` or `3` | Download history |
| `5` / `6` | Popular books / recommendations |
| `?` or F1 | Keys and tips |
| Esc | Back; on the first page of a section, Home |
| `q` or Ctrl+C | Quit; with text selected, Ctrl+C copies it instead |

In book lists:

| Key | Action |
| --- | --- |
| ↑/↓ or `j`/`k` | Select a book |
| PgUp/PgDn, Home/End | Move by a screen, or to the first or last book |
| ←/→ or `p`/`n` | Previous or next page, when the server reports one |
| Enter | Book details (in Booklists, open the list) |
| `m` | More like this: books similar to the selected book |
| `d` | Download the selected book, after a confirmation |
| `x` | Cancel the running download |
| `r` | Refresh, or retry after an error |

With a mouse, click a row to select it and click it again to open it.

In book details, ↑/↓ and PgUp/PgDn scroll a page taller than the terminal, `m`
lists similar books and `r` refreshes. The bottom line of every page shows the
keys that apply there, so it lists ↑↓ for details only when the page scrolls;
when the terminal is narrow, the less important keys give way to `…`.

On the search form, the letter and digit shortcuts are off, and `/` types a
slash in its text fields. Tab moves between fields, Esc leaves the form,
Ctrl+P opens the command dialog and Ctrl+Q or Ctrl+C quits.

## Book lists

Favorites, download history, search results, popular, recommended and similar
books, and the books of a booklist share one layout:

- the page title, such as `Favorites`, `Search: dune` or `Similar to: Dune`;
- a status line: `Page 2 of 5 · Book 3 of 20`, followed by the number of books
  when the server reports it, for example in your favorites or for a search.
  Search results then show their filters and sort order, such as
  `french · EPUB · 2000–2010 · Sort: Year`; when the line is too short, the
  last of these give way first, marked by `…`. Without a reported page count it
  shows `Page 2`; popular, recommended and similar books are not paginated and
  show `Book 3 of 30`;
- a card with the selected book's title, authors, and format, size, year and
  language, plus `★ Saved` when the server says the book is in your favorites.
  Missing fields are left out. The card follows the selection immediately,
  without another request;
- a table numbered across pages, with Title and Author. When every title and
  author on the page fits, Author starts right after the longest title;
  otherwise titles keep at least 60% of the width. Terminals at least 100
  columns wide add Format and Year, unless they would shorten a title that fits
  without them. A title too long for either layout is shortened, and the
  columns stay.

Lists use the server's pages and totals; the TUI never guesses that another page
exists. Popular, recommended and similar books appear as the server returns
them, in one unpaginated list. Some entries include only a title and reference;
an empty title or author shows as a dimmed "Unknown" in the table.

An empty list says so, for example "No books in your favorites yet.". An empty
search also lists the filters you could loosen; a sort order never removes
books, so it is not among them. A failed request shows
`✗ Could not load favorites` and a short reason; press `r` to try again. On
search results, `s` opens the form filled in with that search, so you can change
it. Download history comes from your Z-Library account and does not prove that a
file exists on this computer. These views are distinct from the website's native
My Library page.

## Booklists

Booklists show your lists with their book counts and visibility; a dash marks a
value the server did not report. The card shows the list's title, the first two
lines of its description and its visibility, book count and update date when
known. Enter opens a list's books, titled `Booklists › <name>`.

## Book details

The details page shows where you came from, such as `Favorites › Book details`,
then the title, authors and format line, the publisher, edition, pages, year,
language, ISBN-10 and ISBN-13, identifier, categories, saved state and book
reference that the server provided, and the description, or a dimmed "No
description available.". Esc returns to the list with its page, row and scroll
position.

## Download a book

Press `d` on a selected book, in a list or on its details page. A dialog shows
the title, format, size and the folder it will be saved in, and reminds you
that it uses one download from your daily allowance. Enter on **Download**
starts it; Esc cancels and nothing is sent.

The book is saved into the `download_dir` setting (`zlib-cli config set
download_dir DIR`), else your Downloads folder, else the current folder, under
its title. A line at the bottom shows the progress, then where the file was
saved or why it failed. `x` cancels the running download. One download runs at
a time; a failure is shown and never retried on its own, so pressing `d` again
is a new, explicit download.

Quitting stops a running download: the first `q` warns you, a second one
within five seconds quits. With the private server, unfinished downloads end
with the session. A running `zlib-cli serve` must have downloads enabled
(`--downloads` or `--download-dir DIR`); otherwise the TUI says so.

## Search

Press `s`, type a title, author or ISBN (1–500 characters) and press Enter. The
search form shows every field together:

| Field | Meaning |
| --- | --- |
| Query | Title, author or ISBN. |
| Year from, Year to | Publication years from 1 to 9999, both included. Either may stay empty. |
| Languages | Comma-separated names, such as `english, french`. Empty means any language. |
| Formats | PDF, EPUB and DjVu checkboxes. With none checked, the search includes all three. |
| Sort by | Default (the server's order), best match, popular, recently added, title A–Z or Z–A, year, largest or smallest file first. |

Tab, Shift+Tab, ↑ and ↓ move between fields; ← and → move between the format
checkboxes. Space toggles a format, Enter on Sort by opens its list, and Enter in
any other field or Ctrl+S anywhere runs the search. The `Filters` header counts
the filters and sort order you set, and the line under the form explains the
focused field.

The form checks its values before sending anything. An invalid value is
explained under its field, in the theme's error color, and the message
disappears when you change that field. When the fields do not fit, the form
scrolls to keep the focused field visible.

Search results replace any previous results. Esc on the results returns to the
form, filled in with that search; Esc on the form goes Home without searching,
and what you typed since opening the form is discarded. The previous results
stay available: press `s` from another section to return to them, or `s` on the
results to change the search.

Z-Library applies the filters and the sort order; a domain may ignore some of
them.

## Discover

**Popular** (`5`) lists the books Z-Library reports as popular. **Recommended**
(`6`) lists your account's recommendations, so it needs a server signed in to
your account; with `zlib-cli serve --anonymous` it reports that the server has
no signed-in Z-Library session, as favorites, booklists and download history do.
Press `m` on a book row, or in a book's details, for similar books.

## Navigation

Each section keeps its own pages: switching between sections returns to the page,
row and scroll position you left. Esc goes back one page at a time, then Home;
Esc on Home does nothing. A section keeps at most 32 pages of history, so after
long chains of details and similar books, the title line asks you to go back
before opening another page.

## Display

Text from the server, your search text and error messages are shown literally,
including brackets; terminal escape sequences and control characters are
removed. Long titles are shortened to fit. Covers and external links are not
opened.

The default theme is Tokyo Night; `/theme` switches to any built-in Textual
theme, including light ones, until you quit. Without colors (`--no-color` or
`NO_COLOR`), Textual draws the theme in shades of gray: the selected row stays
highlighted in bold, focused form rows keep their `>` marker and the focused
query box gets a heavier border.

Below 20 rows the card shrinks to one line, and below 16 rows it is hidden. At
60×18, every page remains usable and the search form fits without scrolling
until error messages need more room. Below 40×12, the TUI only asks you to
enlarge the terminal and ignores every key except `q`, Ctrl+Q and Ctrl+C, which
quit.

## Loading and cancellation

Loading runs in the background, with a spinner on the status line. Switching
sections, going Home, searching again or changing pages cancels the old local
read, and results from an old page cannot replace the current one. A request
already accepted by the API may still finish its upstream read. If the account
is busy, wait for that operation to finish and retry. Nothing is retried in the
background; press `r` to try again.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Optional terminal interface is missing | Install `.[tui]` with the same Python interpreter used to launch the CLI. |
| An interactive terminal is required | Run the command in a terminal; use the CLI commands with `--json` for pipes and scripts. |
| Token file is missing or refused | Run `zlib-cli tui` without `--api-url` and `--token-file`, or start `zlib-cli serve` and select its token file. |
| Could not reach the server | Check that the server is running and that its address matches `--api-url`. |
| The server has no signed-in Z-Library session | Run `zlib-cli login` if needed, then restart `zlib-cli serve` without `--anonymous` and press `r`. |
| The local API rejected this request | Press `s` to change the search text or filters, then search again. |
| Z-Library did not complete the request | Wait, then press `r`. |
| The server is busy, or the service is limiting requests | Wait for the current request to finish, then press `r`. |
| No books found for this search | Change the search text or loosen the filters. |
| Enlarge the terminal | Make the window at least 40 columns by 12 rows. |

## Use the local catalog client from Python

`CatalogAPI` uses only the standard library. It exposes the same reads used by
the TUI, returning the existing immutable book, booklist and pagination models:

```python
from pathlib import Path
from zlib_connector import SearchFilters
from zlib_connector.catalog_api import CatalogAPI, search_parameters

catalog = CatalogAPI("http://127.0.0.1:8000/v1", Path("api-token"), timeout=10)
page = catalog.favorites(page=1, limit=20)
for book in page.books:
    print(book.title)

filters = SearchFilters(languages=("french",), formats=("epub",), year_from=2000, order="year")
results = catalog.search("history", page=1, limit=20, filters=filters)
print(results.total_items, results.next_page)

popular = catalog.popular()
recommended = catalog.recommended()
lists = catalog.booklists()
history = catalog.history(page=1, limit=20)
# catalog.similar("ID:HASH")
# catalog.booklist_books("BOOKLIST_ID", page=1)
# catalog.book("ID:HASH")
```

`search` returns a `BookPage`; `popular`, `recommended` and `similar` return
tuples of `Book` in the server's order. `search_parameters(query, page=1,
limit=20, filters=filters)` checks a search the way the local API does and
returns its query pairs without sending anything; invalid values raise
`ValueError` with a message you can show to a user.

Every read method accepts `cancel=CancellationToken()`. Cancelling it interrupts
the local read and raises `TransferCancelled`; it does not change the remote
account. Responses are bounded and validated before being turned into public
models.

The TUI uses one background reader and one replaceable pending request. Shutdown
signals cancellation and waits up to half a second for its reader. A connection
still being established, or a custom adapter that ignores cancellation, may
finish later; it cannot update a closed view or start pending work. Established
catalog reads check cancellation while waiting for headers and body bytes,
including on Windows.
