"""CLI adapter. Credentials are prompted, never accepted as command-line flags."""
import getpass
import json
import re
import shlex
import sys

from .eapi import Client
from .credentials import load_credentials
from .errors import ConnectorError
from .search import SearchFilters, validate_search
from .books import BookReference
from .diagnostics import KNOWN_DOMAINS, diagnose_domains
from .booklists import BooklistSettings, booklist_entry_id, booklist_id, validate_booklist_changes
from .pagination import validate_page
from .my_library import validate_tags
from .http_cli import run_server
from .cli_jobs import run_jobs
from .cli_progress import ProgressDisplay
from .cli_session import AccountSession, local_session_command, session_message
from .transfer import TransferCancelled
from .cli_parser import BOOKLIST_MUTATIONS, CATALOG_COMMANDS, DISCOVERY_COMMANDS, LIBRARY_COMMANDS, parser
from .cli_views import account_confirmation, booklist_options, run_catalog_command, run_library_command, show_books


def prompt(label: str) -> str:
    print(label, end="", file=sys.stderr, flush=True)
    value = sys.stdin.readline()
    if not value:
        raise EOFError
    return value.strip()


def search_filters(args) -> SearchFilters:
    validate_search(args.query, args.page, args.limit)
    return SearchFilters(languages=args.language or (), formats=args.format or (),
                         year_from=args.year_from, year_to=args.year_to, order=args.order)


def shell(client: Client, *, progress=None):
    books = []
    last_search = None
    result = None
    print('Commands: search "query" [filters] | next | prev | download NUMBER [DIRECTORY] | quit')
    while True:
        try:
            args = shlex.split(prompt("zlib> "))
            if not args:
                continue
            if args == ["quit"] or args == ["exit"]:
                return
            if args[0] == "search" and len(args) >= 2:
                # Keep unquoted queries usable; parse options when present.
                search_args = args if any(value.startswith("--") for value in args[1:]) else ["search", " ".join(args[1:])]
                try:
                    options = parser().parse_args(search_args)
                except SystemExit:
                    continue
                filters = search_filters(options)
                result = client.search_page(options.query, filters=filters, page=options.page, limit=options.limit)
                last_search = (options.query, filters, options.limit)
                books = result.books
                show_books(books)
            elif args in (["next"], ["prev"]):
                if last_search is None or result is None:
                    raise ValueError("Run a search first.")
                page = result.next_page if args[0] == "next" else result.previous_page
                if page is None:
                    print("No page available in that direction.")
                    continue
                query, filters, limit = last_search
                result = client.search_page(query, filters=filters, page=page, limit=limit)
                books = result.books
                show_books(books)
            elif args[0] == "download" and len(args) in (2, 3):
                index = int(args[1])
                if not 1 <= index <= len(books):
                    raise ValueError("Choose a number from the most recent search.")
                with ProgressDisplay(progress) as display:
                    path = client.download(books[index - 1], args[2] if len(args) == 3 else ".", progress=display)
                print(path)
            else:
                print('Use: search "query" [filters], next, prev, download NUMBER [DIRECTORY], or quit.', file=sys.stderr)
        except (ConnectorError, ValueError, OSError) as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "jobs":
            run_jobs(args)
            return 0
        if args.command in ("status", "logout"):
            status = local_session_command(args, client_factory=Client)
            print(json.dumps(status) if args.json else session_message(status))
            return 0
        if args.command == "serve":
            run_server(args, client_factory=Client, credentials_loader=load_credentials, prompt=prompt)
            return 0
        if args.command == "domains":
            print(json.dumps(list(KNOWN_DOMAINS)) if args.json else "\n".join(KNOWN_DOMAINS))
            return 0
        if args.command == "doctor":
            domains = args.domains or ([args.domain] if args.domain else None)
            report = diagnose_domains(domains, timeout=args.timeout, workers=args.workers)
            if args.json:
                print(json.dumps(report.to_dict()))
            else:
                for result in report.results:
                    state = "ok" if result.search_ok else result.error_code
                    print(f"{result.domain}  {state}  {result.elapsed_ms} ms  {result.message}")
                print(f"Suggested domain: {report.recommended_domain or 'none'}")
            return 0 if report.recommended_domain else 1
        if args.command == "check":
            domain = args.domain or "https://z-lib.by/"
            with Client(domain, timeout=args.timeout) as client:
                books = client.search("Pride and Prejudice", limit=1)
                print(json.dumps({"reachable": True, "domain": client.domain, "api": "eapi", "search_ok": True, "results": len(books), "authenticated": False}))
            return 0
        filters = None
        reference = None
        favorite_change = args.command == "favorites" and args.favorite_action in ("add", "remove")
        booklist_change = args.command == "booklists" and args.booklist_action in BOOKLIST_MUTATIONS
        tag_change = args.command == "tags" and args.tag_action in ("add", "remove")
        account_change = favorite_change or booklist_change or tag_change
        if account_change and not args.yes and not sys.stdin.isatty():
            raise ValueError("Use --yes to apply an account update in non-interactive mode.")
        if favorite_change:
            reference = args.reference
            if args.page != 1 or args.limit != 20 or args.order != "saved_date":
                raise ValueError("List pagination and ordering cannot be used with a favorite update.")
        if tag_change:
            reference = args.reference
            validate_tags(args.tags, for_removal=args.tag_action == "remove")
        if args.command in ("favorites", "history", "library"):
            validate_page(getattr(args, "page", 1), args.limit)
        if args.command == "booklists" and args.booklist_action in ("show", "add", "remove", "edit", "delete"):
            booklist_id(args.booklist_id)
            if args.booklist_action == "show":
                validate_page(args.page, 20)
            elif args.booklist_action == "add":
                reference = args.reference
            elif args.booklist_action == "remove":
                booklist_entry_id(args.entry_id)
            elif args.booklist_action == "edit":
                validate_booklist_changes(**booklist_options(args))
        if args.command == "booklists" and args.booklist_action == "create":
            BooklistSettings(**booklist_options(args))
        if args.command in DISCOVERY_COMMANDS and not 1 <= args.limit <= 1000:
            raise ValueError("Choose a display limit between 1 and 1000 books.")
        if args.command in ("info", "formats", "similar"):
            reference = args.reference
        if args.command in ("search", "download"):
            filters = search_filters(args)
            if args.command == "download" and (not args.output.is_dir() or (args.pick is not None and args.pick < 1)):
                raise ValueError("Choose an existing output directory and a positive --pick number.")
            if args.command == "download" and (re.match(r"^[0-9]+:", args.query) or "://" in args.query):
                reference = args.query
                if args.pick is not None or filters != SearchFilters() or args.page != 1 or args.limit != 20:
                    raise ValueError("Search filters and --pick cannot be used with a direct book reference.")
        if reference is not None:
            BookReference.parse(reference, args.domain)
        if args.command in CATALOG_COMMANDS and getattr(args, "anonymous", False):
            with Client(args.domain or "https://z-lib.by/", timeout=args.timeout) as client:
                run_catalog_command(client, args, filters)
            return 0
        account = AccountSession(args, credentials_loader=load_credentials)
        with Client(account.domain, timeout=args.timeout) as client:
            if reference is not None:
                BookReference.parse(reference, client.domain)
            if account_change and not args.yes:
                if prompt(account_confirmation(args, client.domain)).lower() not in ("y", "yes"):
                    print("Cancelled.", file=sys.stderr)
                    return 130
            account.authenticate(client, prompt=prompt,
                                 password_prompt=lambda label: getpass.getpass(label, stream=sys.stderr))
            if args.command == "login":
                print(json.dumps({"authenticated": True, "stored": True, "domain": client.domain})
                      if args.json else f"Session saved for {client.domain}.")
                return 0
            if args.command == "auth-test":
                print(json.dumps({"authenticated": True}))
                return 0
            if args.command == "shell":
                shell(client, progress=args.progress)
                return 0
            if args.command in CATALOG_COMMANDS:
                run_catalog_command(client, args, filters)
                return 0
            if args.command in ("profile", "quota"):
                profile = client.get_profile()
                if args.json:
                    value = profile.to_dict() if args.command == "profile" else profile.quota.to_dict()
                    print(json.dumps(value))
                else:
                    for name, value in profile.quota.to_dict().items():
                        print(f"{name.capitalize()}: {value if value is not None else 'unknown'}")
                return 0
            if args.command in LIBRARY_COMMANDS:
                run_library_command(client, args)
                return 0
            if reference is not None:
                selected = reference
            else:
                books = client.search(args.query, filters=filters, page=args.page, limit=args.limit)
                if not books:
                    raise ValueError("No supported books found for this query.")
                if args.pick is None:
                    show_books(books, file=sys.stderr)
                    index = int(prompt("Result number: "))
                else:
                    index = args.pick
                if not 1 <= index <= len(books):
                    raise ValueError("Choose a number from the returned results.")
                selected = books[index - 1]
            with ProgressDisplay(args.progress) as display:
                path = client.download(selected, args.output, progress=display)
            print(json.dumps({"path": str(path)}, ensure_ascii=False) if args.json else path)
        return 0
    except TransferCancelled:
        print(json.dumps({"error": "TransferCancelled", "message": "Cancelled."}) if getattr(args, "json", False)
              else "Cancelled.", file=sys.stderr)
        return 130
    except (ConnectorError, ValueError) as error:
        if getattr(args, "json", False):
            print(json.dumps({"error": type(error).__name__, "message": str(error)}), file=sys.stderr)
        else:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 130
    except OSError:
        print("Local I/O failed. Check the destination and permissions.", file=sys.stderr)
        return 1
