"""Shared account selection and authentication for the CLI and HTTP launcher."""

from .cli_output import CommandError, InputRequired
from .errors import AuthenticationError
from .session_data import canonical_origin
from .sessions import SessionStore
from .cli_settings import cli_domain, named_credentials, notice
from .credentials import credentials_path, is_legacy, default_credentials_file

DEFAULT_DOMAIN = "https://z-lib.by/"


def _legacy_notice(args):
    """Once per run, when the lookup falls back to credentials.local.toml beside the package."""
    if is_legacy(credentials_path(None)):
        notice(args, "Note: using credentials.local.toml beside the installed package. Move it to"
                     f" {default_credentials_file()} (zlib-cli config path shows the folder).")


def add_session_options(parser, commands):
    """--session-file and --no-session are global options (cli_parser.common_options)."""
    for name, help_text in (("login", "sign in and save a private session"),
                            ("logout", "forget the local session without revoking the server session"),
                            ("status", "inspect the local session without connecting")):
        command = commands.add_parser(name, help=help_text)
        if name == "status":
            command.add_argument("--verify", action="store_true", help="verify the saved session with its domain")


def domain_mismatch(message):
    return CommandError(message, code="session_domain_mismatch",
                        hint="Run zlib-cli login on that domain, or use --no-session.")


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
            # The anonymous chain: --domain, ZLIB_DOMAIN, the config file's domain, the default.
            self.domain = args.domain or DEFAULT_DOMAIN
            return
        if self.persist and args.no_session:
            raise ValueError("The login command saves a session; omit --no-session.")
        fresh = args.command in ("login", "auth-test")
        if not args.no_session and args.command != "auth-test":
            self.store = SessionStore(args.session_file)
            self.snapshot = self.store.read()
        saved = self.snapshot.session if self.snapshot is not None else None
        named = named_credentials(args)
        if fresh or named is not None or saved is None:
            if named is None:
                _legacy_notice(args)
            self.credentials = credentials_loader(named)
        loaded = self.credentials
        file_domain = loaded.domain if loaded is not None and getattr(loaded, "has_domain", True) else None
        # Levels 1 to 4: --domain, ZLIB_DOMAIN, the config file, a named credentials file that sets domain.
        chosen, source = args.domain, getattr(args, "domain_source", None) or "--domain"
        if not chosen and named is not None and file_domain:
            chosen, source = file_domain, "the credentials file (domain)"
        # Levels 5 to 7: the saved session, the default credentials file's domain, the default.
        self.domain = chosen or (saved.origin if saved is not None else None) or file_domain or DEFAULT_DOMAIN
        self.restore = saved is not None and not fresh
        if self.restore and chosen and canonical_origin(chosen) != saved.origin:
            raise domain_mismatch(f"The saved session belongs to another domain than the one set by {source}. Run"
                                  " zlib-cli login to sign in there, or use --no-session.")

    @property
    def needs_prompt(self) -> bool:
        """True when no session will be restored and the email or password is missing."""
        if self.anonymous or self.restore:
            return False
        credentials = self.credentials
        return credentials is None or not credentials.email or not credentials.password

    def require_input(self, policy, *, explicit: bool) -> None:
        """Refuse before any request when signing in would need a prompt that the policy forbids.

        ``explicit``: the command exists to sign in (login, auth-test).
        """
        if not self.needs_prompt:
            return
        credentials = self.credentials
        email = credentials is not None and credentials.email
        password = credentials is not None and credentials.password
        if (email or policy.allowed) and (password or policy.password_allowed):
            return
        if explicit:
            raise InputRequired("Signing in needs a terminal to ask for the email and password, or both in the"
                                " credentials file.")
        raise CommandError("No saved session, and no terminal to sign in.", name="AuthenticationError",
                           code="upstream_authentication_required", hint="Run zlib-cli login in a terminal first.")

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
    explicit = cli_domain(args)
    if explicit is not None:
        origin = canonical_origin(explicit)
        if snapshot.session is not None and snapshot.session.origin != origin:
            raise domain_mismatch("The saved session belongs to another domain.")
    elif args.domain and snapshot.session is not None and canonical_origin(args.domain) != snapshot.session.origin:
        # Only an explicit --domain stops these local commands; a configured one gets a note.
        notice(args, f"Note: the saved session belongs to {snapshot.session.origin}, not to the domain set by"
                     f" {getattr(args, 'domain_source', None) or 'the settings'}. Account commands will stop with an"
                     " error until you run zlib-cli login there.")
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
