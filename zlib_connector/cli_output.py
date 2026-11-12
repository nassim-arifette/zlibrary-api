"""CLI output contract: safe streams, ASCII JSON, one error object, documented exit codes.

See docs/cli-json.md. Data goes to stdout; errors, hints and notes go to stderr.
"""
import json
import os
import re
import sys
import traceback

from .error_codes import Coded, ErrorCode, classify
from .errors import ConnectorError
from .terminal_text import terminal_text
from .transfer import TransferCancelled


EXIT_CODES = (
    (0, "Success, including empty results"),
    (1, "Failed: refused, challenge, not found, protocol, local or internal error"),
    (2, "Usage: invalid argument, input required, session for another domain"),
    (3, "Try again later: rate limit, quota, timeout, network, busy lock"),
    (4, "Check first: a change or download may have happened, session conflict"),
    (5, "Sign in first: no usable session or credentials"),
    (130, "Cancelled: Ctrl+C, end of input, declined, cancelled transfer"),
)
_USAGE = frozenset({"invalid_argument", "input_required", "session_domain_mismatch"})
_SIGN_IN = frozenset({"upstream_authentication_required"})
_LATER = frozenset({"upstream_rate_limited", "upstream_timeout", "upstream_network_error", "download_unavailable",
                    "session_busy", "client_busy", "server_busy", "downloads_busy", "download_not_ready"})
_CHECK = frozenset({"session_conflict", "mutation_uncertain"})

_HINTS = {
    "booklist_not_found": "Run: zlib-cli booklists",
    "upstream_authentication_required": "Run: zlib-cli login",
    "upstream_challenge": "Run: zlib-cli doctor",
    "upstream_rate_limited": "Check: zlib-cli quota",
    "upstream_network_error": "Run: zlib-cli doctor",
    "session_busy": "Try again in a moment.",
    "session_conflict": "Run: zlib-cli status",
    "download_unavailable": "Check: zlib-cli quota",
    "internal_error": "Set ZLIB_DEBUG=1 to show details.",
    "unauthorized": "Use the token file of the running server (--token-file).",
    "downloads_unavailable": "Start the server with --download-dir DIR.",
    "download_conflict": "Use a new --key for a different book.",
}
QUOTA_HINT = ("The download may have used quota. Check zlib-cli quota and zlib-cli history before downloading this"
              " book again.")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_IO_MESSAGE = "Local I/O failed. Check the destination and permissions."
_INTERNAL_MESSAGE = "Unexpected error. Set ZLIB_DEBUG=1 to show details."


class UsageError(ValueError):
    """A command-line usage error; argparse's message, exit 2."""


class CommandError(ValueError, Coded):
    """A CLI failure with a chosen code. ``name`` is the JSON ``error`` value (ValueError by default)."""

    def __init__(self, message, *, code="invalid_argument", hint=None, name="ValueError", retryable=False,
                 outcome="not_applied", reason=None):
        super().__init__(message)
        self.code, self.hint, self.name = code, hint, name
        self.retryable, self.outcome, self.reason = retryable, outcome, reason

    def classification(self) -> ErrorCode:
        return ErrorCode(self.code, retryable=self.retryable, outcome=self.outcome)


class InputRequired(CommandError):
    """A prompt would be needed, but input is not allowed."""

    def __init__(self, message, *, name="InputRequired", hint=None):
        super().__init__(message, code="input_required", name=name, hint=hint)


class Cancelled(Exception, Coded):
    """Declined confirmation, q at a choice, or end of input at a prompt."""

    def classification(self) -> ErrorCode:
        return ErrorCode("cancelled")


class _SerializationError(Exception, Coded):
    def classification(self) -> ErrorCode:
        return ErrorCode("internal_error")


def prepare_streams() -> None:
    """Never crash on a legacy code page: characters it lacks become backslash escapes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="backslashreplace")
            except (AttributeError, OSError, TypeError, ValueError):
                pass


def print_json(value, *, indent=None, file=None) -> None:
    try:
        text = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=indent)
    except (TypeError, ValueError, RecursionError):
        raise _SerializationError() from None
    stream = sys.stdout if file is None else file
    stream.write(text + "\n")
    stream.flush()


def wants_json(argv) -> bool:
    """JSON mode: the exact token --json anywhere before a -- separator."""
    for value in argv:
        if value == "--":
            return False
        if value == "--json":
            return True
    return False


def exit_code_for(code, outcome="not_applied", quota_may_be_used=False, retryable=False) -> int:
    if code == "cancelled":
        return 130
    if outcome == "unknown" or quota_may_be_used:
        return 4
    if code in _USAGE:
        return 2
    if code in _SIGN_IN:
        return 5
    if code in _LATER:
        return 3
    if code in _CHECK:
        return 4
    return 3 if retryable else 1


def exit_codes_text() -> str:
    return "exit codes:\n" + "\n".join(f"  {number:<4} {text}" for number, text in EXIT_CODES) + (
        "\nJSON output and error objects: docs/cli-json.md")


def _name_and_message(error):
    if isinstance(error, CommandError):
        return error.name, str(error)
    if isinstance(error, UsageError):
        return "UsageError", str(error)
    if isinstance(error, Cancelled):
        return "Cancelled", str(error) or "Cancelled."
    if isinstance(error, (KeyboardInterrupt, EOFError)):
        return "Cancelled", "Cancelled."
    if isinstance(error, TransferCancelled):
        return "TransferCancelled", "Cancelled."
    if isinstance(error, _SerializationError):
        return "InternalError", _INTERNAL_MESSAGE
    if isinstance(error, (ConnectorError, ValueError)):
        return type(error).__name__, str(error)
    if isinstance(error, OSError):
        return "OSError", _IO_MESSAGE
    return "InternalError", _INTERNAL_MESSAGE


def _mutation_hint(args):
    command = getattr(args, "command", None)
    if command == "favorites":
        return "Check with: zlib-cli favorites"
    if command == "tags":
        return "Check with: zlib-cli tags saved-ids"
    if command == "booklists" and getattr(args, "booklist_action", None) in ("add", "remove"):
        identifier = str(getattr(args, "booklist_id", ""))
        if re.fullmatch(r"[0-9]{1,20}", identifier):  # Already validated before the change.
            return f"Check with: zlib-cli booklists show {identifier}"
    return "Check with: zlib-cli booklists"


def describe(error, *, operation="read", args=None, command=None) -> dict:
    """The error object of docs/cli-json.md for one failure."""
    result = classify(error, mutation=operation == "account_change", download=operation == "download")
    name, message = _name_and_message(error)
    code = result.code if _CODE.fullmatch(result.code or "") else "internal_error"
    exit_code = exit_code_for(code, result.outcome, bool(result.quota_may_be_used), result.retryable)
    value = {"error": name, "message": message, "code": code, "exit_code": exit_code,
             "retryable": bool(result.retryable), "outcome": result.outcome}
    reason = getattr(error, "reason", None)
    if isinstance(reason, str) and _CODE.fullmatch(reason):
        value["reason"] = reason
    if result.quota_may_be_used is not None:
        value["quota_may_be_used"] = result.quota_may_be_used
    hint = getattr(error, "hint", None) if isinstance(error, CommandError) else None
    if operation == "account_change" and result.outcome == "unknown":
        hint = _mutation_hint(args)
    elif result.quota_may_be_used:
        hint = QUOTA_HINT
    elif hint is None and code == "invalid_argument":
        command = command or getattr(args, "command", None)
        hint = f"Run: zlib-cli {command} --help" if command else "Run: zlib-cli --help"
    elif hint is None:
        hint = _HINTS.get(code)
    if hint:
        value["hint"] = hint
    return value


def _debug() -> bool:
    return (os.environ.get("ZLIB_DEBUG") or "").strip() not in ("", "0")


def report_error(error, *, json_mode=False, operation="read", args=None, command=None) -> int:
    """Write one error (a JSON line in JSON mode) on stderr and return the exit status."""
    value = describe(error, operation=operation, args=args, command=command)
    try:
        if json_mode:
            print_json(value, file=sys.stderr)
        else:
            if value["code"] in ("cancelled", "local_io_error"):
                line = value["message"]  # The text printed before the error contract.
            else:
                line = f"{value['error']}: {value['message']}"
            lines = [terminal_text(line)]
            hint = value.get("hint")
            if hint and hint not in value["message"]:
                lines.append("Hint: " + terminal_text(hint))
            print("\n".join(lines), file=sys.stderr, flush=True)
        if value["code"] == "internal_error" and _debug():
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
    except (OSError, ValueError):
        pass  # A closed stderr must not hide the exit status.
    return value["exit_code"]
