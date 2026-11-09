"""Shared account selection and authentication for the CLI and HTTP launcher."""
from pathlib import Path

from .errors import AuthenticationError
from .session_data import canonical_origin
from .sessions import SessionStore


def add_session_options(parser, commands):
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--session-file", type=Path,
                         help="private saved session (default: state directory/session.json)")
    options.add_argument("--no-session", action="store_true", help="do not read or save persistent sessions")
    for name, help_text in (("login", "sign in and save a private session"),
                            ("logout", "forget the local session without revoking the server session"),
                            ("status", "inspect the local session without connecting")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true")
        if name == "status":
            command.add_argument("--verify", action="store_true", help="verify the saved session with its domain")


class AccountSession:
    """Resolve a domain now, authenticate only when the caller is ready.

    Listener binding and account-change confirmation belong to the caller.
    Saved sessions never cause a fallback password login after a restore error.
    Only the explicit login command writes persistent account state.
    """

    def __init__(self, args, *, credentials_loader):
        self.anonymous = getattr(args, "anonymous", False)
        self.persist = args.command == "login"
        self.store = None
        self.snapshot = None
        self.credentials = None
        if self.anonymous:
            self.domain = args.domain or "https://z-lib.by/"
            return
        if self.persist and args.no_session:
            raise ValueError("The login command saves a session; omit --no-session.")
        fresh = args.command in ("login", "auth-test")
        if not args.no_session and args.command != "auth-test":
            self.store = SessionStore(args.session_file)
            self.snapshot = self.store.read()
        saved = self.snapshot.session if self.snapshot is not None else None
        if fresh or args.credentials is not None or saved is None:
            self.credentials = credentials_loader(args.credentials)
        configured = self.credentials.domain if self.credentials is not None else None
        self.domain = args.domain or (configured if args.credentials is not None else None) or (
            saved.origin if saved is not None else configured)
        self.restore = saved is not None and not fresh
        if self.restore and canonical_origin(self.domain) != saved.origin:
            raise ValueError("The saved session belongs to another domain. Use login or --no-session explicitly.")

    def authenticate(self, client, *, prompt, password_prompt):
        if self.anonymous:
            return
        if self.restore:
            self.credentials = None
            client.restore_session(self.store)
            return
        credentials, self.credentials = self.credentials, None
        email, password = credentials.email, credentials.password
        del credentials
        try:
            email = email or prompt("Email: ")
            password = password or password_prompt("Password: ")
            options = {"session_store": self.store} if self.persist else {}
            client.login(email, password, **options)
        finally:
            email = password = ""


def local_session_command(args, *, client_factory):
    """Inspect/forget local state without consulting the credentials file."""
    if args.no_session:
        raise ValueError("This command manages saved sessions; omit --no-session.")
    store = SessionStore(args.session_file)
    snapshot = store.read()
    if args.domain is not None:
        origin = canonical_origin(args.domain)
        if snapshot.session is not None and snapshot.session.origin != origin:
            raise ValueError("The saved session belongs to another domain.")
    if args.command == "logout":
        return store.clear(expected_generation=snapshot.generation).status()
    status = snapshot.status()
    if args.verify:
        if snapshot.session is None or snapshot.session.expired:
            raise AuthenticationError("No usable saved session. Run login to connect explicitly.")
        with client_factory(snapshot.session.origin, timeout=args.timeout) as client:
            return client.restore_session(store)
    return status


def session_message(status):
    if status.get("authenticated"):
        return f"Session verified for {status['domain']}."
    if status["state"] == "empty":
        return "No saved session."
    if status["state"] == "expired":
        return f"Saved session expired for {status['domain']}. Run login to reconnect."
    return f"Session saved for {status['domain']}; not verified with Z-Library."
