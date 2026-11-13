"""CLI adapter. Credentials are prompted, never accepted as command-line flags."""
import getpass
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
from .mcp_cli import run_mcp
from .cli_completion import glue, run_complete
from .cli_jobs import run_jobs, run_queue
from .cli_tui import run_tui
from .cli_progress import ProgressDisplay, Ticker
from .cli_batch import is_batch, run_batch_command
from .cli_config import run_config
from .cli_settings import cli_domain, client_factory, default_output, ensure_settings, notice, with_configured_filters
from .cli_session import AccountSession, local_session_command, session_message
from .cli_table import print_table
from .cli_examples import DEPRECATED
from .cli_output import CommandError, InputRequired, UsageError, prepare_streams, print_json, report_error, wants_json
from .cli_terminal import InputPolicy, ask, choose, is_terminal, password
from .cli_parser import (BOOKLIST_MUTATIONS, CATALOG_COMMANDS, DISCOVERY_COMMANDS, LIBRARY_COMMANDS, command_names,
                         finish, parser)
from .cli_changes import prepare as prepare_change, run_change
from .cli_views import booklist_options, run_catalog_command, run_library_command, show_books


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

def _announce(args, cli_filters):
    """The stderr notice for configured formats or languages, once, just before the search request."""
    if cli_filters is not None:
        with_configured_filters(args, cli_filters)


class _Operation:
    """Which call is running, for classify(): a read, an account change or a download."""

    def __init__(self):
        self.kind = "read"
        self.args = None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    prepare_streams()
    if argv[:1] == ["__complete"]:
        return run_complete(argv[1:])  # Hidden; never loads settings, never reports errors.
    json_mode = wants_json(argv)
    operation = _Operation()
    try:
        return _run(argv, operation)
    except (Exception, KeyboardInterrupt) as error:
        return report_error(error, json_mode=json_mode, operation=operation.kind, args=operation.args,
                            command=_command_name(argv))


def _command_name(argv):
    """The first known command in argv, for the usage hint; never arbitrary text."""
    known = set(command_names())
    for value in argv:
        if value == "--":
            break
        if value in known:
            return value
    return None


def _run(argv, operation) -> int:
    command_parser = parser()
    try:
        args, extras = command_parser.parse_known_args(argv)
    except UsageError as error:
        if getattr(error, "help_text", None) and not wants_json(argv):
            sys.stderr.write(error.help_text + "\n")
        raise
    finish(args)
    operation.args = args
    if extras:
        # Titles may follow options: download "Dune" --format epub "Neuromancer".
        if args.command != "download" or any(value.startswith("-") for value in extras):
            command_parser.error("unrecognized arguments: " + " ".join(extras))
        args.query = [*args.query, *extras]
    if args.command == "mcp":
        return run_mcp(args, client_factory=Client)  # Loads the settings itself; refusals keep their one line.
    if args.command == "config":
        return run_config(args)
    if args.command == "completion":
        sys.stdout.write(glue(args.shell))
        return 0
    ensure_settings(args)
    make_client = client_factory(args, Client)
    policy = InputPolicy.from_args(args)

    def ask_line(label):
        return ask(label, policy=policy, read=prompt)

    def ask_password(label):
        return password(label, policy=policy, read=lambda text: getpass.getpass(text, stream=sys.stderr))

    if args.command == "download":
        args.output = default_output(args)
        if is_batch(args):
            return run_batch_command(args, client_factory=make_client, credentials_loader=load_credentials,
                                     prompt=ask_line, password_prompt=ask_password, policy=policy)
        args.query = args.query[0]
        args.page = 1 if args.page is None else args.page
        args.limit = 20 if args.limit is None else args.limit
    if args.command == "tui":
        run_tui(args)
        return 0
    if args.command == "jobs":
        return run_jobs(args)
    if args.command == "queue":
        return run_queue(args)
    if args.command in ("status", "logout"):
        status = local_session_command(args, client_factory=make_client)
        if args.json:
            print_json(status)
        else:
            print("Local session forgotten. The server session is not revoked." if args.command == "logout"
                  else session_message(status))
        return 0
    if args.command == "serve":
        run_server(args, client_factory=make_client, credentials_loader=load_credentials, prompt=ask_line,
                   password_prompt=ask_password, policy=policy)
        return 0
    if args.command == "domains":
        if args.json:
            print_json(list(KNOWN_DOMAINS))
        else:
            print("\n".join(KNOWN_DOMAINS))
        return 0
    if args.command == "doctor":
        # doctor finds working domains: it ignores the environment's and the config file's domain.
        explicit = cli_domain(args)
        domains = args.domains or ([explicit] if explicit else None)
        ticker = Ticker(args.progress is True or (args.progress is None and is_terminal(sys.stderr)))
        try:
            report = diagnose_domains(domains, timeout=args.timeout, workers=args.workers, proxy=args.proxy,
                                      on_result=lambda done, total: ticker(f"Checked {done} of {total}"))
        finally:
            ticker.close()
        if args.json:
            print_json(report.to_dict())
        else:
            print_table(("DOMAIN", "STATE", "TIME", "MESSAGE"),
                        [(result.domain, "ok" if result.search_ok else result.error_code, f"{result.elapsed_ms} ms",
                          result.message) for result in report.results], flexible=(3,), right=(2,))
            print(f"Suggested domain: {report.recommended_domain or 'none'}")
            if report.proxy is not None:
                print(f"Through proxy: {report.proxy} ({args.settings.source_label('proxy')})")
            configured = args.settings.domain if explicit is None else None
            if configured is not None:
                origin = "config" if args.settings.origin("domain") == "config" else args.domain_source
                print(f"Configured domain: {configured} ({origin})")
                if report.recommended_domain and report.recommended_domain != configured:
                    print(f"To use it: zlib-cli config set domain {report.recommended_domain}")
        return 0 if report.recommended_domain else 1
    if args.command in DEPRECATED and not args.json:
        notice(args, DEPRECATED[args.command])
    if args.command == "check":
        domain = args.domain or "https://z-lib.by/"
        with make_client(domain, timeout=args.timeout) as client:
            books = client.search("Pride and Prejudice", limit=1)
            print_json({"reachable": True, "domain": client.domain, "api": "eapi", "search_ok": True,
                        "results": len(books), "authenticated": False})
        return 0
    if args.command == "shell" and not policy.allowed:
        raise InputRequired("The shell needs a terminal. Use search and download in scripts, or add --interactive"
                            " to read commands from standard input.")
    filters = None
    cli_filters = None  # A search's own filters; configured ones are announced just before it runs.
    reference = None
    favorite_change = args.command == "favorites" and args.favorite_action in ("add", "remove")
    booklist_change = args.command == "booklists" and args.booklist_action in BOOKLIST_MUTATIONS
    tag_change = args.command == "tags" and args.tag_action in ("add", "remove")
    account_change = favorite_change or booklist_change or tag_change
    if account_change:
        prepare_change(args)
    listed = account_change and args.change_items is not None
    if listed and not args.yes and not args.dry_run:
        raise InputRequired("A list of account changes needs --yes. Use --dry-run to see it first.",
                            name="ValueError")
    if account_change and not args.yes and not args.dry_run and not policy.allowed:
        raise InputRequired("Use --yes to apply an account update in non-interactive mode.", name="ValueError")
    if favorite_change and not listed:
        reference = args.reference
    if favorite_change and (args.page != 1 or args.limit != 20 or args.order != "saved_date"):
        raise ValueError("List pagination and ordering cannot be used with a favorite update.")
    if tag_change:
        reference = None if listed else args.reference
        validate_tags(args.tags, for_removal=args.tag_action == "remove")
    if args.command in ("favorites", "history", "library"):
        validate_page(getattr(args, "page", 1), args.limit)
    if args.command == "booklists" and args.booklist_action in ("show", "add", "remove", "edit", "delete"):
        booklist_id(args.booklist_id)
        if args.booklist_action == "show":
            validate_page(args.page, 20)
        elif args.booklist_action == "add" and not listed:
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
            # Only filters given on the command line conflict; configured ones never apply to a reference.
            if args.pick is not None or filters != SearchFilters() or args.page != 1 or args.limit != 20:
                raise ValueError("Search filters and --pick cannot be used with a direct book reference.")
        else:
            cli_filters = filters
            filters = with_configured_filters(args, filters, announce=False)
        if args.command == "download" and reference is None and args.pick is None and not policy.allowed:
            raise InputRequired("Choosing a search result needs a terminal. Add --pick N, or download an ID:HASH"
                                " reference.")
    if reference is not None:
        BookReference.parse(reference, args.domain)
    if args.command in CATALOG_COMMANDS and getattr(args, "anonymous", False):
        with make_client(args.domain or "https://z-lib.by/", timeout=args.timeout) as client:
            _announce(args, cli_filters)
            run_catalog_command(client, args, filters)
        return 0
    account = AccountSession(args, credentials_loader=load_credentials)
    account.require_input(policy, explicit=args.command in ("login", "auth-test"))
    with make_client(account.domain, timeout=args.timeout) as client:
        if reference is not None:
            BookReference.parse(reference, client.domain)
        saved = account.snapshot.session if account.snapshot is not None else None
        account.authenticate(client, prompt=ask_line, password_prompt=ask_password)
        if args.command == "login":
            if saved is not None and saved.origin != client.domain:
                notice(args, f"Note: this replaced the session saved for {saved.origin}.")
            if args.json:
                print_json({"authenticated": True, "stored": True, "domain": client.domain})
            else:
                print(f"Session saved for {client.domain}.")
            return 0
        if args.command == "auth-test":
            print_json({"authenticated": True})
            return 0
        if args.command == "shell":
            shell(client, progress=args.progress)
            return 0
        if args.command in CATALOG_COMMANDS:
            _announce(args, cli_filters)
            run_catalog_command(client, args, filters)
            return 0
        if args.command in ("profile", "quota"):
            profile = client.get_profile()
            if args.json:
                print_json(profile.to_dict() if args.command == "profile" else profile.quota.to_dict())
            elif args.command == "profile":
                print(f"Account on {client.domain}")
                print("Download quota (today)")
                for name, value in profile.quota.to_dict().items():
                    print(f"  {name.capitalize() + ':':<11}{value if value is not None else 'unknown'}")
            else:
                for name, value in profile.quota.to_dict().items():
                    print(f"{name.capitalize()}: {value if value is not None else 'unknown'}")
            return 0
        if args.command in LIBRARY_COMMANDS:
            if account_change:
                return run_change(client, args, policy=policy, read=prompt, operation=operation)
            run_library_command(client, args)
            return 0
        if reference is not None:
            selected = reference
        else:
            _announce(args, cli_filters)
            books = client.search(args.query, filters=filters, page=args.page, limit=args.limit)
            if not books:
                raise CommandError("No supported books found for this query.", code="not_found")
            if args.pick is None:
                show_books(books, file=sys.stderr)
                index = choose(len(books), policy=policy, read=prompt)
            else:
                index = args.pick
            if not 1 <= index <= len(books):
                raise ValueError("Choose a number from the returned results.")
            selected = books[index - 1]
        operation.kind = "download"
        with ProgressDisplay(args.progress) as display:
            path = client.download(selected, args.output, progress=display)
        operation.kind = "read"
        if args.json:
            print_json({"path": str(path)})
        else:
            print(path)
    return 0
