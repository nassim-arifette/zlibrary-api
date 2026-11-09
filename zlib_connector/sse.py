"""Bounded SSE decoding and validation of the local task API's public schema."""
import json
import math

from .errors import NetworkError
from .jobs.events import validate_event_id
from .jobs.models import TASK_ERROR_CODES, TASK_STATES, task_error_message, validate_reference, validate_task_id
from .transfer import TransferProgress


FRAME_LIMIT = 64 * 1024
_TASK_FIELDS = {"id", "reference", "state", "created_at", "updated_at", "progress",
                "cancel_requested", "error_code", "file_available", "error_message", "outcome"}
_PROGRESS_FIELDS = {"bytes_received", "total_bytes", "elapsed_seconds", "bytes_per_second", "eta_seconds", "fraction"}


def invalid_stream():
    raise NetworkError("The local API returned an invalid task event stream.") from None


def _number(value, maximum=None):
    try:
        return (type(value) in (int, float) and value >= 0 and math.isfinite(value)
                and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def _integer(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_value):
    raise ValueError


def event_json(data):
    try:
        value = json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        invalid_stream()


def validate_public_event(value, *, identifier, cursor):
    """Reject private/unknown fields instead of reflecting arbitrary server data."""
    try:
        if (set(value) != {"id", "task", "paused"} or type(value["paused"]) is not bool
                or validate_event_id(value["id"]) != cursor):
            raise ValueError
        task = value["task"]
        if not isinstance(task, dict) or set(task) != _TASK_FIELDS:
            raise ValueError
        if validate_task_id(task["id"]) != identifier:
            raise ValueError
        validate_reference(task["reference"])
        state, code = task["state"], task["error_code"]
        if (state not in TASK_STATES or (code is not None and code not in TASK_ERROR_CODES)
                or not _number(task["created_at"], 2**63 - 1)
                or not _number(task["updated_at"], 2**63 - 1)
                or task["updated_at"] < task["created_at"]
                or type(task["cancel_requested"]) is not bool
                or type(task["file_available"]) is not bool
                or task["file_available"] != (state == "succeeded")
                or (state == "succeeded" and code is not None)
                or task["error_message"] != task_error_message(code)):
            raise ValueError
        outcome = "not_started" if state in ("queued", "waiting_quota") else "completed" if state == "succeeded" else "unknown"
        if task["outcome"] != outcome:
            raise ValueError
        progress = task["progress"]
        if progress is not None:
            if (not isinstance(progress, dict) or set(progress) != _PROGRESS_FIELDS
                    or not _integer(progress["bytes_received"])
                    or (progress["total_bytes"] is not None and not _integer(progress["total_bytes"]))
                    or not _number(progress["elapsed_seconds"])
                    or any(item is not None and not _number(item) for item in
                           (progress["bytes_per_second"], progress["eta_seconds"], progress["fraction"]))):
                raise ValueError
            expected = TransferProgress(progress["bytes_received"], progress["total_bytes"], progress["elapsed_seconds"]).to_dict()
            if progress != expected:
                raise ValueError
        return value
    except (ValueError, TypeError, KeyError, OverflowError):
        invalid_stream()


class SSEDecoder:
    """Incremental UTF-8 parser; incomplete EOF is deliberately not dispatched.

    Memory is bounded by a 64 KiB frame (including comments/ignored fields).
    A frame may use LF, CRLF or CR; the first UTF-8 BOM is optional.
    """

    def __init__(self):
        self._line = bytearray()
        self._size = 0
        self._after_cr = False
        self._first_line = True
        self._event = None
        self._id = None
        self._data = []

    def feed(self, chunk):
        for byte in chunk:
            self._size += 1
            if self._size > FRAME_LIMIT:
                invalid_stream()
            if self._after_cr:
                self._after_cr = False
                if byte == 10:
                    continue
            if byte in (10, 13):
                self._after_cr = byte == 13
                event = self._finish_line()
                if event is not None:
                    yield event
            else:
                self._line.append(byte)
                if len(self._line) > FRAME_LIMIT:
                    invalid_stream()

    def _finish_line(self):
        line = bytes(self._line)
        self._line.clear()
        if self._first_line:
            self._first_line = False
            if line.startswith(b"\xef\xbb\xbf"):
                line = line[3:]
        try:
            text = line.decode("utf-8")
        except UnicodeError:
            invalid_stream()
        if not text:
            event = None
            if self._data:
                event = (self._event, self._id, "\n".join(self._data))
            elif self._event is not None or self._id is not None:
                invalid_stream()
            self._event, self._id, self._data, self._size = None, None, [], 0
            return event
        if text.startswith(":"):
            return None
        name, _, value = text.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        elif name == "event":
            if self._event is not None:
                invalid_stream()
            self._event = value
        elif name == "id":
            if self._id is not None or "\x00" in value:
                invalid_stream()
            self._id = value
        return None
