# Collections TUI

Browse your favorites, personal booklists and Z-Library download history in an
interactive terminal. Open a book for its description and available metadata,
then return to the selected row. This first version reads collections; search,
editing and download-task views will follow.

## Start

From the project folder:

```sh
python -m pip install -e ".[tui]"
```

In one terminal, start the authenticated local API:

```sh
zlib-cli serve
```

Keep that terminal open. In a second terminal:

```sh
zlib-cli tui
```

`serve` handles your Z-Library session. The TUI reads its existing local bearer
token and connects only to the loopback API. It does not ask for your Z-Library
password, start a server, follow redirects or use HTTP proxy settings.

For a different server address or token file, pass the same values you chose
when starting the server:

```sh
zlib-cli tui --api-url http://127.0.0.1:8123/v1 --token-file ./api-token
zlib-cli tui --view history --page-size 30
zlib-cli --timeout 10 tui --no-color
```

The starting view can be `favorites`, `booklists` or `history`. Page size is
1–50 for favorites and history; the service controls booklist page size. The
global `--timeout` goes before `tui`. `--no-color` and the `NO_COLOR` environment
variable enable monochrome display.

## Navigate

| Key | Action |
| --- | --- |
| F / B / H | Favorites, booklists, download history (1 / 2 / 3 also work). |
| Up / Down, Enter | Select a row and open its list or book. |
| Esc | Return to the previous view, retaining its page and selected row. |
| P / N | Previous or next available page. |
| R | Refresh the current view. |
| ? or F1 | Show navigation help. |
| Ctrl+P | Open the command palette, including theme selection. |
| Q | Quit from a collection or detail view. |

The visible buttons also work with a mouse. Book details scroll with the arrow
and page keys. Returning to a collection restores its position; refreshing asks
the server for a new copy of the current page.

## What you see

Favorites and download history show the pages reported by the server. A missing
total stays unknown. Next and previous controls use the available navigation;
the TUI does not guess that another page exists because the current page is full.

Booklists show your lists, their known book counts and visibility. Opening a list
shows its members; opening a book shows its details. Download history comes from
your Z-Library account and does not prove that a file exists on this computer.
These views are distinct from the website's native My Library page.

Text is displayed literally, including brackets in titles. Covers and external
links are not opened. The book-detail view can scroll when the description is
long. Use the command palette to choose a light or dark theme.

Loading runs in the background. Switching views or refreshing cancels the old
local read, and results from an old view cannot replace the current one. A
request already accepted by the API may still finish its upstream read. If the
account is busy, wait for that operation to finish and refresh explicitly.
There are no automatic retries.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Optional terminal interface is missing | Install `.[tui]` with the same Python interpreter used to launch the CLI. |
| An interactive terminal is required | Run the command in a terminal; use collection commands with `--json` for pipes and scripts. |
| Token file is missing or refused | Start `zlib-cli serve` and select its existing token file. |
| Could not contact the local API | Check that the server is running and that its address matches `--api-url`. |
| Account session is missing or expired | Sign in again using the CLI and restart the server. |
| Account is busy or the request timed out | Wait for the current request to finish, then refresh. |
| Empty collection | Select another collection or refresh after adding books through the website or CLI. |

## Use the local catalog client from Python

`CatalogAPI` uses only the standard library. It exposes the same reads used by
the TUI, returning the existing immutable book, booklist and pagination models:

```python
from pathlib import Path
from zlib_connector.catalog_api import CatalogAPI

catalog = CatalogAPI("http://127.0.0.1:8000/v1", Path("api-token"), timeout=10)
page = catalog.favorites(page=1, limit=20)
for book in page.books:
    print(book.title)

lists = catalog.booklists()
history = catalog.history(page=1, limit=20)
# catalog.booklist_books("BOOKLIST_ID", page=1)
# catalog.book("ID:HASH")
```

Every method accepts `cancel=CancellationToken()`. Cancelling it interrupts the
local read and raises `TransferCancelled`; it does not change the remote account.
Responses are bounded and validated before being turned into public models.

The TUI uses one background reader and one replaceable pending request. Shutdown
signals cancellation and waits up to half a second for its reader. A connection
still being established, or a custom adapter that ignores cancellation, may
finish later; it cannot update a closed view or start pending work. Established
catalog reads check cancellation while waiting for headers and body bytes,
including on Windows.
