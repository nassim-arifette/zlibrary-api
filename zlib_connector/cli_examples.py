"""One table for the command help: summary, description and examples of every command.

A test parses every example with ``cli_parser.parser()``, so the examples stay valid.
"""

GROUPS = (
    ("Find", ("search", "info", "formats", "similar", "recommended", "popular", "recent")),
    ("Download", ("download", "queue", "jobs")),
    ("Library", ("library", "favorites", "history", "booklists", "tags")),
    ("Account", ("login", "logout", "status", "profile", "quota")),
    ("Site", ("doctor", "domains")),
    ("Tools", ("config", "serve", "tui", "shell", "completion", "mcp")),
)
HIDDEN = ("check", "auth-test")
DEPRECATED = {
    "check": "Note: check is deprecated; use zlib-cli doctor DOMAIN.",
    "auth-test": "Note: auth-test is deprecated; use zlib-cli login, then zlib-cli status --verify.",
}

# name: (summary, description, examples)
COMMANDS = {
    "search": ("search the catalog",
               "Search titles, authors or ISBNs. Configured languages and formats apply unless given here.",
               ('search "dune" --format epub', 'search "le petit prince" --language french --order year',
                'search "fluent python" --json --with-pagination')),
    "info": ("show one book's details", "Read the full details of an exact ID:HASH reference or book URL.",
             ("info 1234567:a1b2c3", "info 1234567:a1b2c3 --json")),
    "formats": ("list a book's other formats", "List the other files of the same book, with their references.",
                ("formats 1234567:a1b2c3", "formats 1234567:a1b2c3 --json")),
    "similar": ("books similar to a book", "Browse books the server lists as similar to an exact reference.",
                ("similar 1234567:a1b2c3", "similar 1234567:a1b2c3 --limit 5 --json")),
    "recommended": ("books recommended for the account", "Browse the server's recommendations for your account.",
                    ("recommended", "recommended --limit 10 --json")),
    "popular": ("popular books", "Browse the books the server lists as popular.",
                ("popular", "popular --anonymous --limit 10")),
    "download": ("download one book or a list",
                 "Download an exact reference, a search result chosen with a prompt or --pick, or a list with -a.",
                 ("download 1234567:a1b2c3 --output books", 'download "dune" --format epub --pick 1',
                  "download -a reading-list.txt --dry-run", "download -a reading-list.csv --yes --json")),
    "jobs": ("manage downloads on a running local server",
             "Submit, follow and collect download tasks of zlib-cli serve --downloads.",
             ("jobs add 1234567:a1b2c3 --key reading-0001", "jobs list", "jobs watch 0123456789abcdef0123456789abcdef")),
    "library": ("overview of favorites, history and booklists", "Show the first books of each account collection.",
                ("library", "library --limit 10 --json")),
    "favorites": ("browse or change account favorites",
                  "List favorites, or add and remove books. Changes ask for confirmation, or need --yes;"
                  " --dry-run shows a change without sending it.",
                  ("favorites", "favorites add 1234567:a1b2c3 --dry-run",
                   "favorites add 1234567:a1b2c3 --yes", "favorites remove -a favorites.json --yes")),
    "history": ("browse the account download history", "List the books downloaded with the account.",
                ("history", "history --limit 50 --json")),
    "booklists": ("browse or change account booklists",
                  "List, read, create, edit or delete booklists, and add or remove books.",
                  ("booklists", "booklists show 98765", "booklists add 98765 1234567:a1b2c3 --yes",
                   "booklists delete 98765 --dry-run")),
    "tags": ("native My Library tags", "List tags and saved book IDs, or add and remove tags on a book.",
             ("tags", "tags saved-ids --json", 'tags add 1234567:a1b2c3 "sci-fi" --yes',
              'tags add -a books.json "sci-fi" --yes')),
    "login": ("sign in and save a private session",
              "Sign in with the credentials file or prompts, then save the session for later commands.",
              ("login", "login --domain https://z-lib.by/")),
    "logout": ("forget the local session", "Delete the saved session. The server session is not revoked.",
               ("logout", "logout --json")),
    "status": ("inspect the local session", "Show the saved session without connecting, or check it with --verify.",
               ("status", "status --verify --json")),
    "profile": ("show the account profile", "Show the account's labeled sections, such as the download quota.",
                ("profile", "profile --json")),
    "quota": ("show the download quota", "Show today's download limit, used and remaining downloads.",
              ("quota", "quota --json")),
    "doctor": ("find domains where search works",
               "Run an anonymous search on known or given domains and suggest one. Nothing is changed.",
               ("doctor", "doctor https://z-lib.by/ --json", "doctor --workers 2")),
    "domains": ("list known domain candidates", "List the known candidates without probing them.",
                ("domains", "domains --json")),
    "config": ("show and change saved settings", "Read and write config.toml. It never stores secrets.",
               ("config list --show-origin", "config set domain https://z-lib.by/", "config set formats epub pdf",
                "config unset timeout")),
    "serve": ("serve the local HTTP API", "Serve the authenticated local HTTP API on a loopback address.",
              ("serve", "serve --port 8080 --downloads", "serve --anonymous")),
    "tui": ("interactive terminal interface", "Browse collections, search and discover books in a terminal UI.",
            ("tui", "tui --view search", "tui --api-url http://127.0.0.1:8000/v1")),
    "shell": ("interactive search and download loop", "Log in once, then search and download repeatedly.",
              ("shell", "shell --no-progress")),
    "mcp": ("MCP server for AI assistants", "Serve MCP tools over stdio. Read-only unless downloads or changes are enabled.",
            ("mcp", "mcp --download-dir books", "mcp --print-config claude-code")),
    "check": ("deprecated: use doctor DOMAIN", "Check public HTTPS access without credentials.", ("check",)),
    "auth-test": ("deprecated: use login, then status --verify", "Test login, then discard the session.",
                  ("auth-test",)),
}


def examples_text(name: str) -> str:
    _, _, examples = COMMANDS[name]
    return "Examples:\n" + "\n".join(f"  zlib-cli {example}" for example in examples)


def groups_text(summaries: dict) -> str:
    """The grouped command list with one summary line per command; ``summaries`` maps the shown commands."""
    lines = []
    width = max(len(name) for name in summaries) + 2
    for title, names in GROUPS:
        present = [name for name in names if name in summaries]
        if present:
            lines.append(f"{title}:")
            lines.extend(f"  {name:<{width}}{summaries[name] or ''}".rstrip() for name in present)
    return "\n".join(lines)
