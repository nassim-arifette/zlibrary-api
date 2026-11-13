"""`zlib-cli mcp`: serve MCP tools over stdio. Read-only unless actions are enabled by flags.

Startup validates every flag locally (no network, no prompts, no credentials),
then diverts the standard descriptors so that only protocol messages reach stdout.
"""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import threading

from .eapi import Client
from .mcp_server.backend import Backend
from .mcp_server.protocol import McpServer, ShutdownRequested, SUPPORTED_VERSIONS, divert_stdio
from .mcp_server.tools import INSTRUCTIONS, SERVER_NAME, SERVER_TITLE, ToolSet, select_tools


DEFAULT_DOMAIN = "https://z-lib.by/"
CLIENTS = ("claude-code", "claude-desktop", "codex")
# Clients pass only a short allowlist of variables: copy the ones that change what the server resolves.
_ENVIRONMENT = ("ZLIB_STATE_DIR", "XDG_STATE_HOME", "ZLIB_CONFIG", "XDG_CONFIG_HOME", "ZLIB_DOMAIN", "ZLIB_TIMEOUT",
                "ZLIB_PROXY")


def server_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("zlib-connector")
    except Exception:
        return "0.1.0"


def add_mcp_parser(commands):
    command = commands.add_parser(
        "mcp", help="serve MCP tools over stdio for AI assistants; read-only unless enabled",
        description="Serve Model Context Protocol tools over stdin/stdout for an AI assistant such as Claude Code, "
                    "Claude Desktop or Codex. Register this command in the client; see docs/mcp.md.",
        epilog="Actions are off by default: without flags only read-only tools are listed. --download-dir enables "
               "downloads into that folder, and --allow-account-changes enables changes to favorites, tags and "
               "booklists. Global options such as --domain, --timeout and --session-file work before or after 'mcp'.")
    command.add_argument("--anonymous", action="store_true",
                         help="never read or send the saved session; list public read tools only")
    command.add_argument("--download-dir", type=Path, metavar="DIR",
                         help="enable the download tools; DIR (an existing folder) is the only place files are written")
    command.add_argument("--no-download-confirmation", action="store_true",
                         help="with --download-dir: do not ask Claude Code to confirm each download_book and "
                              "resume_downloads call (account changes still ask)")
    command.add_argument("--allow-account-changes", action="store_true",
                         help="enable tools that change favorites, tags and booklists (each call asks for approval)")
    command.add_argument("--print-config", choices=CLIENTS, metavar="CLIENT",
                         help="print a registration snippet for claude-code, claude-desktop or codex, then exit")
    return command


class _Refused(Exception):
    pass


def _validate(args, *, probe=True):
    """Return (domain, store, download_dir) or raise _Refused with a one-line message."""
    from .batch import check_folder
    from .jobs.files import TaskFiles
    from .jobs.models import JobError
    from .session_data import canonical_origin
    from .sessions import SessionStore

    if getattr(args, "credentials", None) is not None:
        raise _Refused("The mcp command never reads credentials; omit --credentials and run zlib-cli login instead.")
    if getattr(args, "no_session", False):
        raise _Refused("The mcp command uses the saved session or --anonymous; omit --no-session.")
    if args.anonymous and (args.download_dir is not None or args.allow_account_changes
                           or args.session_file is not None):
        raise _Refused("--anonymous cannot be combined with --download-dir, --allow-account-changes or --session-file.")
    if args.no_download_confirmation and args.download_dir is None:
        raise _Refused("--no-download-confirmation requires --download-dir.")
    from .cli_settings import ensure_settings
    from .settings import SettingsError
    try:
        # The domain and timeout follow the CLI's precedence: option, ZLIB_*, config file, default.
        # A configured download_dir never enables the download tools: --download-dir stays explicit.
        ensure_settings(args)
    except SettingsError as error:
        raise _Refused(str(error)) from None
    timeout = args.timeout
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise _Refused("The request timeout must be between 0 and 300 seconds.")
    if args.domain is not None:
        try:
            canonical_origin(args.domain)
        except ValueError:
            raise _Refused("Choose an HTTPS site origin for --domain, for example https://z-lib.by/.") from None
    download_dir = None
    if args.download_dir is not None:
        folder = args.download_dir.expanduser()
        if not folder.is_dir() or folder.is_symlink():
            raise _Refused("Choose an existing download directory, not a link.")
        try:
            TaskFiles(folder)
            # The hard-link probe writes two short-lived files; --print-config writes nothing.
            download_dir = check_folder(folder) if probe else folder.resolve(strict=True)
        except (ValueError, OSError, JobError) as error:
            raise _Refused(str(error) if isinstance(error, (ValueError, JobError))
                           else "Choose an existing download directory, not a link.") from None
    store = None
    domain = args.domain
    if not args.anonymous:
        store = SessionStore(args.session_file)
        try:
            snapshot = store.read()
        except Exception:
            snapshot = None  # Reported by server_status; it does not stop startup.
        saved = snapshot.session if snapshot is not None else None
        if saved is not None:
            if domain is not None and canonical_origin(domain) != saved.origin:
                source = getattr(args, "domain_source", None) or "--domain"
                raise _Refused(f"The saved session belongs to another domain than the one set by {source}. Drop it,"
                               " or run zlib-cli login on that domain.")
            domain = domain or saved.origin
    domain = domain or DEFAULT_DOMAIN
    return domain, store, download_dir


def _command_arguments(args, download_dir):
    from .cli_settings import cli_domain
    argv = []
    if cli_domain(args) is not None:
        argv += ["--domain", cli_domain(args)]
    if getattr(args, "config", None) is not None:
        argv += ["--config", str(Path(args.config).expanduser().absolute())]
    if getattr(args, "no_config", False):
        argv.append("--no-config")
    settings = getattr(args, "settings", None)
    if getattr(args, "no_proxy", False):
        argv.append("--no-proxy")
    elif settings is not None and settings.origin("proxy") == "cli":
        argv += ["--proxy", settings.proxy]
    timeout = args.timeout
    if download_dir is not None and timeout == 30:
        timeout = 120
    argv += ["--timeout", format(timeout, "g")]
    if args.session_file is not None:
        argv += ["--session-file", str(args.session_file.expanduser().absolute())]
    argv.append("mcp")
    if args.anonymous:
        argv.append("--anonymous")
    if download_dir is not None:
        argv += ["--download-dir", str(download_dir)]
    if args.no_download_confirmation:
        argv.append("--no-download-confirmation")
    if args.allow_account_changes:
        argv.append("--allow-account-changes")
    return argv, timeout


def _toml(value):
    if "'" not in value and not any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "'" + value + "'"
    return json.dumps(value, ensure_ascii=False)


def print_config(args, download_dir, out):
    """Write a registration snippet for one client; make no request and write no file."""
    argv, timeout = _command_arguments(args, download_dir)
    command = str(Path(sys.executable).absolute())
    full = ["-m", "zlib_connector", *argv]
    env = {name: os.environ[name] for name in _ENVIRONMENT if os.environ.get(name)}
    client = args.print_config
    if client == "claude-code":
        entry = {"type": "stdio", "command": command, "args": full}
        if env:
            entry["env"] = env
        out.write(json.dumps(entry, indent=2, ensure_ascii=False) + "\n")
        hint = "Register it with: claude mcp add-json zlib '<the JSON above>' --scope user"
    elif client == "claude-desktop":
        entry = {"command": command, "args": full}
        if env:
            entry["env"] = env
        out.write(json.dumps({"mcpServers": {"zlib": entry}}, indent=2, ensure_ascii=False) + "\n")
        hint = "Merge it into claude_desktop_config.json (Settings > Developer > Edit Config), then restart Claude Desktop."
    else:
        tool_timeout = max(60, math.ceil(timeout) + 60)
        lines = ["[mcp_servers.zlib]", f"command = {_toml(command)}",
                 "args = [" + ", ".join(_toml(item) for item in full) + "]",
                 "startup_timeout_sec = 20",
                 f"tool_timeout_sec = {tool_timeout}",
                 'default_tools_approval_mode = "writes"   # prompts for every tool not marked read-only']
        prompted = []
        if download_dir is not None:
            if args.no_download_confirmation:
                lines += ["# --no-download-confirmation: download_book and resume_downloads have no per-tool prompt",
                          "# block here; default_tools_approval_mode still asks for every tool not marked read-only."]
            else:
                prompted += ["download_book", "resume_downloads"]
        if args.allow_account_changes:
            prompted += [tool.name for tool in select_tools(account_changes=True) if tool.group == "account"]
        for name in prompted:
            lines += ["", f"[mcp_servers.zlib.tools.{name}]", 'approval_mode = "prompt"']
        if env:
            lines += ["", "[mcp_servers.zlib.env]"] + [f"{name} = {_toml(value)}" for name, value in env.items()]
        out.write("\n".join(lines) + "\n")
        hint = "Add it to ~/.codex/config.toml (or a trusted project's .codex/config.toml)."
    out.flush()
    return hint


def _logger(stream):
    logger = logging.Logger("zlib-mcp", logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("zlib-mcp: %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def run_mcp(args, *, client_factory=Client, stdin=None, stdout=None, stderr=None) -> int:
    """Validate, then serve until EOF. stdin/stdout are binary streams; when omitted the real descriptors are diverted."""
    errors = stderr if stderr is not None else sys.stderr
    try:
        domain, store, download_dir = _validate(args, probe=not args.print_config)
    except _Refused as refusal:
        print(f"zlib-cli mcp: {refusal}", file=errors, flush=True)
        return 1
    if args.print_config:
        out = sys.stdout if stdout is None else stdout
        hint = print_config(args, download_dir, out)
        print(hint, file=errors, flush=True)
        return 0
    from .cli_settings import client_factory as with_proxy
    client_factory = with_proxy(args, client_factory)
    logger = _logger(errors)
    version = server_version()
    backend = Backend(domain=domain, timeout=args.timeout, client_factory=client_factory, store=store,
                      anonymous=args.anonymous, download_dir=download_dir, logger=logger)
    tools = ToolSet(backend, downloads=download_dir is not None, account_changes=args.allow_account_changes,
                    download_confirmation=not args.no_download_confirmation, version=version, logger=logger)
    server = McpServer(tools, server_info={"name": SERVER_NAME, "title": SERVER_TITLE, "version": version},
                       instructions=INSTRUCTIONS, logger=logger)
    previous = None
    try:
        if stdin is None or stdout is None:
            stdin, stdout = divert_stdio()
        if threading.current_thread() is threading.main_thread() and os.name != "nt":
            def stop(signum, frame):
                raise ShutdownRequested()
            previous = signal.signal(signal.SIGTERM, stop)
        groups = ["read"] + (["downloads"] if download_dir is not None else []) + (
            ["account changes"] if args.allow_account_changes else [])
        logger.info("%s %s, protocol %s, tools: %s (%d)%s", SERVER_NAME, version, " and ".join(SUPPORTED_VERSIONS),
                    ", ".join(groups), len(tools.tools), ", anonymous" if args.anonymous else "")
        try:
            interactive = stdin.isatty()
        except (AttributeError, ValueError, OSError):
            interactive = False
        if interactive:
            logger.info("This command speaks MCP on stdin/stdout; register it in an MCP client (docs/mcp.md).")
        server.serve(stdin, stdout)
    except (KeyboardInterrupt, ShutdownRequested):
        server.shutdown()
    finally:
        try:
            backend.close()
        except (KeyboardInterrupt, ShutdownRequested):
            pass
        if previous is not None:
            try:
                signal.signal(signal.SIGTERM, previous)
            except (ValueError, OSError):
                pass
        logger.info("stopped")
    return 0
