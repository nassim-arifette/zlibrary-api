"""The nine account changes: described prompts, dry runs and lists of references.

A change is described after sign-in, with reads only: the book title, the booklist title and its
book count. A list change needs --yes, applies each reference once in input order and never
retries; a rate limit stops it and leaves the remaining items not started.
"""
import json
from pathlib import Path
import sys

from .batch_input import MAX_INPUT_BYTES, BatchInputError, parse_input
from .booklists import validate_booklist_changes
from .cli_output import mutation_hint, print_json
from .cli_settings import notice
from .cli_table import print_table
from .cli_terminal import confirm
from .cli_views import booklist_options, run_library_command
from .error_codes import classify
from .errors import AuthenticationError, BooklistNotFoundError, ChallengeRequired, ConnectorError, RateLimitError
from .my_library import validate_tags
from .terminal_text import terminal_text

ACTIONS = {
    ("favorites", "add"): "favorite_add", ("favorites", "remove"): "favorite_remove",
    ("tags", "add"): "tags_add", ("tags", "remove"): "tags_remove",
    ("booklists", "add"): "booklist_add", ("booklists", "remove"): "booklist_remove",
    ("booklists", "create"): "booklist_create", ("booklists", "edit"): "booklist_edit",
    ("booklists", "delete"): "booklist_delete",
}
LIST_ACTIONS = ("favorite_add", "favorite_remove", "tags_add", "tags_remove", "booklist_add")
ENTRY_PAGES = 3  # Booklist pages read to identify an entry before removing it.
TITLE_CHARS = 60
_STOPS = ("upstream_rate_limited", "upstream_authentication_required", "upstream_challenge")
TITLE_REFUSED = ("{label} is a title, not a reference. Account changes take ID:HASH, a book URL, or objects with"
                 " book_id and book_hash. To find references: zlib-cli download -a LIST --dry-run --json")


def change_action(args) -> str | None:
    sub = {"favorites": "favorite_action", "tags": "tag_action", "booklists": "booklist_action"}.get(args.command)
    return ACTIONS.get((args.command, getattr(args, sub, None))) if sub else None


def read_references(source=None, *, data: bytes | None = None) -> tuple[str, ...]:
    """Exact ID:HASH references from stdin (-), a file, or given bytes; a title or an empty list is refused."""
    if data is not None:
        items, _ = parse_input(data)
    elif source == "-":
        stream = getattr(sys.stdin, "buffer", None)
        data = stream.read(MAX_INPUT_BYTES + 1) if stream is not None else sys.stdin.read(MAX_INPUT_BYTES + 1)
        items, _ = parse_input(data)
    else:
        items, _ = parse_input(Path(source))
    references = []
    for item in items:
        label = item.position[:1].upper() + item.position[1:]
        if item.error is not None:
            raise BatchInputError(item.error)
        if item.reference is None:
            raise BatchInputError(TITLE_REFUSED.format(label=label))
        references.append(item.reference)
    if not references:
        raise BatchInputError("The list has no references.")
    return tuple(references)


def prepare(args) -> None:
    """Validate the change before credentials load; a list is read now (args.change_items, else None)."""
    action = change_action(args)
    args.change_items = None
    if args.yes and args.dry_run:
        raise ValueError("--yes cannot be combined with --dry-run, which never changes the account.")
    if action not in LIST_ACTIONS:
        return
    source = getattr(args, "batch_file", None)
    if source is not None and action.startswith("tags_") and args.reference not in (None, "-"):
        args.tags, args.reference = [args.reference, *args.tags], None  # -a FILE TAG...: the first word is a tag.
    if source is not None and args.reference is not None:
        raise ValueError("Give one reference, - for standard input, or -a FILE, not several.")
    if source is None and args.reference is None:
        raise ValueError("Give a reference, - to read a list from standard input, or -a FILE.")
    if args.reference == "-":
        source = "-"
    if source is not None:
        args.change_items = read_references(source)


def _short(text) -> str | None:
    if not text:
        return None
    text = " ".join(terminal_text(str(text)).split())
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 3] + "..."


def _quoted(text) -> str:
    return json.dumps(text, ensure_ascii=False)


def _soft(read):
    """A read used only for the description; a failure leaves the detail unknown."""
    try:
        return read()
    except (AuthenticationError, ChallengeRequired, RateLimitError):
        raise
    except ConnectorError:
        return None


class _Reader:
    def __init__(self, client):
        self.client = client
        self._lists = None

    def book(self, reference) -> tuple[str, str | None]:
        details = _soft(lambda: self.client.get_book(reference))
        return reference.split(":", 1)[0], _short(details.title) if details is not None else None

    def booklist(self, identifier, *, required=False):
        if self._lists is None:
            self._lists = _soft(self.client.get_booklists) or []
        for item in self._lists:
            if item.booklist_id == identifier:
                return item
        if required:
            raise BooklistNotFoundError("This booklist was not found in the account's editable lists.")
        return None

    def entry(self, identifier, entry_id):
        for page in range(1, ENTRY_PAGES + 1):
            result = _soft(lambda: self.client.get_booklist_books(identifier, page=page))
            if result is None:
                return None
            for book in result.books:
                if book.entry_id == entry_id:
                    return book
            if result.next_page is None:
                return None
        return None


def describe(client, args) -> dict:
    """The change that would be sent, with what the reads found (None when unknown)."""
    action, reader = change_action(args), _Reader(client)
    change = {"dry_run": True, "action": action, "domain": client.domain}
    if action in ("favorite_add", "favorite_remove", "tags_add", "tags_remove", "booklist_add"):
        change["book_id"], change["title"] = reader.book(args.reference)
        if action.startswith("tags_"):
            change["tags"] = list(validate_tags(args.tags, for_removal=action == "tags_remove"))
            change["adds_to_favorites"] = action == "tags_add"
    if action.startswith("booklist_") and action != "booklist_create":
        identifier = str(args.booklist_id)
        found = reader.booklist(identifier, required=action == "booklist_edit")
        change["booklist_id"] = identifier
        change["booklist_title"] = _short(found.title) if found is not None else None
        change["book_count"] = found.book_count if found is not None else None
    if action == "booklist_remove":
        entry = reader.entry(str(args.booklist_id), str(args.entry_id))
        change["entry_id"] = str(args.entry_id)
        change["book_id"] = entry.book_id if entry is not None else None
        change["title"] = _short(entry.title) if entry is not None else None
    if action == "booklist_create":
        change["settings"] = booklist_options(args)
    if action == "booklist_edit":
        change["changes"] = validate_booklist_changes(**booklist_options(args))
    return change


def _book(change) -> str:
    if change.get("title"):
        return f"{_quoted(change['title'])} (book {change['book_id']})"
    return f"book {change['book_id']}"


def _booklist(change) -> str:
    text = f"booklist {change['booklist_id']}"
    if change.get("booklist_title"):
        text += f" {_quoted(change['booklist_title'])}"
    count = change.get("book_count")
    if count is not None:
        text += f" ({count} book{'' if count == 1 else 's'})"
    return text


def _settings_text(settings: dict) -> str:
    parts = []
    if settings.get("title") is not None:
        parts.append(f"title {_quoted(_short(settings['title']))}")
    if settings.get("is_public") is not None:
        parts.append("public" if settings["is_public"] else "private")
    if settings.get("color") is not None:
        parts.append(f"color {settings['color']}")
    if settings.get("description") is not None:
        parts.append(f"description {_quoted(_short(settings['description']))}" if settings["description"]
                     else "no description")
    return ", ".join(parts)


def sentence(change) -> tuple[str, str]:
    """The change in words, and what to add after the question mark."""
    action, domain = change["action"], change["domain"]
    tags = _quoted(change.get("tags"))
    if action == "favorite_add":
        return f"Add {_book(change)} to favorites on {domain}", ""
    if action == "favorite_remove":
        return f"Remove {_book(change)} from favorites on {domain}", ""
    if action == "tags_add":
        return f"Add tags {tags} to {_book(change)} on {domain}", " This also adds the book to favorites."
    if action == "tags_remove":
        return f"Remove tags {tags} from {_book(change)} on {domain}", ""
    if action == "booklist_add":
        return f"Add {_book(change)} to {_booklist(change)} on {domain}", ""
    if action == "booklist_remove":
        entry = f"entry {change['entry_id']} (book not identified)"
        if change.get("title"):
            entry = f"entry {change['entry_id']} ({_quoted(change['title'])}, book {change['book_id']})"
        elif change.get("book_id"):
            entry = f"entry {change['entry_id']} (book {change['book_id']})"
        return f"Remove {entry} from {_booklist(change)} on {domain}", ""
    if action == "booklist_delete":
        return f"Permanently delete {_booklist(change)} on {domain}", " This cannot be undone."
    if action == "booklist_create":
        settings = dict(change["settings"])
        title = _quoted(_short(settings.pop("title")))
        return f"Create booklist {title} on {domain} ({_settings_text(settings)})", ""
    return f"Edit {_booklist(change)} on {domain}: {_settings_text(change['changes'])}", ""


def question(change) -> str:
    text, extra = sentence(change)
    return f"{text}?{extra} [y/N] "


def run_change(client, args, *, policy, read, operation) -> int:
    if args.change_items is not None:
        return _run_list(client, args, operation)
    if args.yes:  # No question to ask, so no reads to describe the change.
        operation.kind = "account_change"
        run_library_command(client, args)
        return 0
    change = describe(client, args)
    if args.dry_run:
        if args.json:
            print_json(change)
        else:
            text, extra = sentence(change)
            print(f"Dry run, nothing sent: {text}.{extra}")
        return 0
    if not args.yes:
        confirm(question(change), policy=policy, read=read)
    operation.kind = "account_change"  # Only the mutating Client call runs from here.
    run_library_command(client, args)
    return 0


def _list_sentence(args, domain, count, booklist) -> str:
    action, books = change_action(args), f"{count} book{'' if count == 1 else 's'}"
    if action == "favorite_add":
        return f"Add {books} to favorites on {domain}"
    if action == "favorite_remove":
        return f"Remove {books} from favorites on {domain}"
    if action in ("tags_add", "tags_remove"):
        tags = _quoted(list(args.tags))
        if action == "tags_add":
            return f"Add tags {tags} to {books} on {domain}; this also adds them to favorites"
        return f"Remove tags {tags} from {books} on {domain}"
    return f"Add {books} to {_booklist(booklist)} on {domain}"


def _mutation(client, args):
    action = change_action(args)
    if action in ("favorite_add", "favorite_remove"):
        return lambda reference: client.set_favorite(reference, saved=action == "favorite_add")
    if action == "tags_add":
        return lambda reference: client.add_book_tags(reference, args.tags)
    if action == "tags_remove":
        return lambda reference: client.remove_book_tags(reference, args.tags)
    return lambda reference: client.add_booklist_book(args.booklist_id, reference)


def _run_list(client, args, operation) -> int:
    action, references = change_action(args), args.change_items
    if action.startswith("tags_"):
        args.tags = list(validate_tags(args.tags, for_removal=action == "tags_remove"))
    booklist = None
    if action == "booklist_add":
        found = _Reader(client).booklist(str(args.booklist_id))
        booklist = {"booklist_id": str(args.booklist_id), "booklist_title": _short(found.title) if found else None,
                    "book_count": found.book_count if found else None}
    summary = _list_sentence(args, client.domain, len(references), booklist)
    if args.dry_run:
        if args.json:
            value = {"dry_run": True, "action": action, "domain": client.domain, "count": len(references),
                     "references": list(references)}
            if action.startswith("tags_"):
                value["tags"] = list(args.tags)
            if booklist is not None:
                value["booklist_id"] = booklist["booklist_id"]
            print_json(value)
        else:
            print(f"Dry run, nothing sent: {summary}.")
            print("\n".join(references))
        return 0
    notice(args, f"{summary}.")
    apply = _mutation(client, args)
    items = [{"position": number, "reference": reference, "status": "not_started", "code": None}
             for number, reference in enumerate(references, 1)]
    cancelled = False
    operation.kind = "account_change"
    for item in items:
        try:
            apply(item["reference"])
        except KeyboardInterrupt:
            item.update(status="uncertain", code="cancelled")
            cancelled = True
            break
        except Exception as error:  # Every item gets a status; nothing is retried.
            result = classify(error, mutation=True)
            if result.code == "upstream_rate_limited":
                item["code"] = result.code
                break
            item.update(status="uncertain" if result.outcome == "unknown" else "failed", code=result.code)
            if result.code in _STOPS:
                break
        else:
            item["status"] = "applied"
    operation.kind = "read"
    counts = {name: sum(item["status"] == name for item in items)
              for name in ("applied", "failed", "uncertain", "not_started")}
    if args.json:
        print_json({"items": items, "summary": counts})
    else:
        print_table(("#", "REFERENCE", "STATUS", "CODE"),
                    [(item["position"], item["reference"], item["status"].replace("_", " "), item["code"])
                     for item in items], right=(0,))
        print(f"Applied {counts['applied']}, failed {counts['failed']}, uncertain {counts['uncertain']},"
              f" not started {counts['not_started']}.")
    if counts["uncertain"]:
        notice(args, f"Some changes may have been applied. {mutation_hint(args)}")
    if cancelled:
        return 130
    if counts["failed"] or counts["uncertain"]:
        return 4
    return 3 if counts["not_started"] else 0
