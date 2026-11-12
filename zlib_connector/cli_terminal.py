"""One terminal test and one input policy for every CLI prompt.

Line prompts (email, confirmation, result choice, shell) need --interactive, or
stdin and stderr that are both terminals, and neither --no-input nor
ZLIB_NO_INPUT. A password is read only from a terminal, never from a pipe.
"""
import ctypes
from dataclasses import dataclass
import os
import re
import sys

from .cli_output import Cancelled, InputRequired


def is_terminal(stream) -> bool:
    try:
        if not stream.isatty():
            return False
    except (AttributeError, OSError, ValueError):
        return False
    return _console(stream)


def _console(stream) -> bool:
    """On Windows, isatty() is also true for NUL: a terminal is a stream with a console mode."""
    if os.name != "nt":
        return True
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return True  # No descriptor to check, as with a substituted stream: trust isatty().
    try:
        import msvcrt
        get_mode = ctypes.WinDLL("kernel32", use_last_error=True).GetConsoleMode
        get_mode.argtypes, get_mode.restype = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)), ctypes.c_int
        mode = ctypes.c_uint32()
        return bool(get_mode(msvcrt.get_osfhandle(descriptor), ctypes.byref(mode)))
    except (AttributeError, ImportError, OSError, ValueError, ctypes.ArgumentError):
        return False


def _disabled(environ) -> bool:
    value = (environ.get("ZLIB_NO_INPUT") or "").strip()
    return value not in ("", "0")


@dataclass(frozen=True)
class InputPolicy:
    allowed: bool  # Line prompts may read stdin.
    password_allowed: bool  # getpass may read the terminal.

    @classmethod
    def from_args(cls, args, *, environ=None, stdin=None, stderr=None) -> "InputPolicy":
        environ = os.environ if environ is None else environ
        stdin = sys.stdin if stdin is None else stdin
        stderr = sys.stderr if stderr is None else stderr
        if getattr(args, "interactive", False):
            allowed = True
        elif getattr(args, "no_input", False) or _disabled(environ):
            allowed = False
        else:
            allowed = is_terminal(stdin) and is_terminal(stderr)
        return cls(allowed=allowed, password_allowed=allowed and is_terminal(stdin))


def _note(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def ask(label: str, *, policy: InputPolicy, read) -> str:
    if not policy.allowed:
        raise InputRequired("This step needs a terminal to ask a question. Use the matching option instead.")
    return read(label)


def password(label: str, *, policy: InputPolicy, read) -> str:
    if not policy.password_allowed:
        raise InputRequired("A password is read only from a terminal. Put it in the credentials file instead.")
    return read(label)


def confirm(question: str, *, policy: InputPolicy, read) -> None:
    """Return when the answer is y or yes; any other answer, EOF included, cancels."""
    try:
        answer = ask(question, policy=policy, read=read)
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise Cancelled("Cancelled. Nothing was changed.")


def choose(count: int, *, policy: InputPolicy, read) -> int:
    """Ask for a number from 1 to count until one is given; q, EOF and Ctrl+C cancel."""
    while True:
        try:
            answer = ask(f"Choose 1-{count}, or q to cancel: ", policy=policy, read=read).strip().lower()
        except EOFError:
            raise Cancelled("Cancelled.") from None
        if answer == "q":
            raise Cancelled("Cancelled.")
        if re.fullmatch(r"[0-9]{1,6}", answer) and 1 <= int(answer) <= count:
            return int(answer)
        _note(f"Type a number from 1 to {count}, or q.")
