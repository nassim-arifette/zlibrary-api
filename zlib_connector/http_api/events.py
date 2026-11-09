"""Bounded SSE frames for public full-state download observations."""

import json

from ..jobs.events import validate_event_id
from ..jobs.models import TERMINAL_STATES


MAX_FRAME_SIZE = 64 * 1024
HEARTBEAT_INTERVAL = 5
END_RESERVE = 0.05


def task_frame(event):
    identifier = validate_event_id(event.id)
    payload = json.dumps(event.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    frame = f"event: task\nid: {identifier}\ndata: {payload}\n\n".encode("utf-8")
    if len(frame) > MAX_FRAME_SIZE:
        raise ValueError("The task observation exceeds the frame limit.")
    return frame


def end_frame(reason):
    if reason not in ("terminal", "duration", "shutdown"):
        raise ValueError("The stream end reason is invalid.")
    return ('event: end\ndata: {"reason":"' + reason + '"}\n\n').encode("ascii")
