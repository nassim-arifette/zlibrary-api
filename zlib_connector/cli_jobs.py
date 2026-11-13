"""CLI operations on the download tasks of an existing local API server."""
from contextlib import closing
import math
import sys
from pathlib import Path
import time

from .cli_output import CommandError, print_json
from .cli_table import print_table
from .cli_settings import default_output, ensure_settings, notice
from .error_codes import classify
from .errors import NetworkError
from .jobs.events import validate_event_id
from .local_api import LocalAPI, task_id
from .local_state import state_directory
from .jobs.models import TERMINAL_STATES, validate_idempotency_key, validate_task_page


def _server_options(command):
    command.add_argument("--api-url", help="loopback HTTP API address (default: the api_url setting, else"
                         " http://127.0.0.1:8000/v1)")
    command.add_argument("--token-file", type=Path, help="existing server token file (default: state directory/api-token)")


def add_queue_parser(commands):
    command = commands.add_parser("queue", help="queue several downloads on a running local API server")
    _server_options(command)
    actions = command.add_subparsers(dest="queue_action", required=True)
    add = actions.add_parser("add")
    add.add_argument("references", nargs="*", metavar="ID:HASH",
                     help="exact references or book URLs, or - to read a list from standard input")
    add.add_argument("-a", "--batch-file", metavar="FILE",
                     help="read references from a TXT, CSV or JSON file, such as the --json output of search,"
                          " favorites or booklists show")
    add.add_argument("--key", required=True, metavar="PREFIX",
                     help="request key prefix: item N is sent as PREFIX-000N; reuse it only to replay the same list")
    listing = actions.add_parser("list")
    listing.add_argument("--limit", type=int, default=50, help="maximum tasks shown (default: 50)")
    listing.add_argument("--offset", type=int, default=0, help="tasks to skip, newest first (default: 0)")
    actions.add_parser("resume")


def add_jobs_parser(commands):
    command = commands.add_parser("jobs", help="manage downloads on a running local API server")
    _server_options(command)
    actions = command.add_subparsers(dest="job_action", required=True)
    for name in ("add", "list", "show", "cancel", "resume", "file", "watch"):
        action = actions.add_parser(name)
        if name in ("show", "cancel", "file", "watch"):
            action.add_argument("task_id", type=task_id)
        if name == "add":
            action.add_argument("reference", help="ID:HASH or a book URL on the server's selected domain")
            action.add_argument("--key", required=True, help="unique request key; reuse it only to replay the same submission")
        if name == "list":
            action.add_argument("--limit", type=int, default=50, help="maximum tasks shown (default: 50)")
            action.add_argument("--offset", type=int, default=0, help="tasks to skip, newest first (default: 0)")
        if name == "file":
            action.add_argument("--output", type=Path, help="existing directory for a validated local copy (default:"
                                " the download_dir setting, else the current directory)")
        if name == "watch":
            action.add_argument("--duration", type=float, default=60, help="total observation time, 1-3600 seconds (default: 60)")
            action.add_argument("--after", help="last observed event cursor; reconnects receive current state")


_UNAVAILABLE = {
    "add": ("unknown", "Run the same command again with the same --key: the server replays it and never adds a"
                       " second download."),
    "cancel": ("unknown", "Check with: zlib-cli jobs show {task_id}"),
    "resume": ("unknown", "Check with: zlib-cli jobs list"),
}
_READ_HINT = "Start the server: zlib-cli serve --download-dir DIR"


_QUEUE_STOPS = ("unauthorized", "forbidden", "downloads_unavailable", "local_api_unavailable")
_REPLAY = "Run the same command again: the server replays each key and never adds a second download."


def run_queue(args) -> int:
    """queue list and resume are jobs list and resume; queue add submits one task per reference."""
    if args.queue_action != "add":
        args.job_action = args.queue_action
        return run_jobs(args)
    ensure_settings(args)
    from .cli_changes import read_references
    if args.batch_file is not None and args.references:
        raise ValueError("Give references, - for standard input, or -a FILE, not several.")
    if args.batch_file is not None:
        references = read_references(args.batch_file)
    elif args.references == ["-"]:
        references = read_references("-")
    elif not args.references or "-" in args.references:
        raise ValueError("Give references, - to read a list from standard input, or -a FILE.")
    else:
        references = read_references(data="\n".join(args.references).encode("utf-8"))
    width = max(4, len(str(len(references))))
    keys = [validate_idempotency_key(f"{args.key}-{number:0{width}d}") for number in range(1, len(references) + 1)]
    api = LocalAPI(args.api_url, args.token_file or state_directory() / "api-token", timeout=args.timeout)
    notice(args, f"Queue {len(references)} download(s) on the local API.")
    items = [{"position": number, "reference": reference, "key": key, "task_id": None, "state": "not_started",
              "code": None} for number, (reference, key) in enumerate(zip(references, keys), 1)]
    cancelled = paused = False
    for item in items:
        try:
            task = api.request("POST", "/downloads", body={"reference": item["reference"],
                                                           "idempotency_key": item["key"]})
        except KeyboardInterrupt:
            item.update(state="uncertain", code="cancelled")
            cancelled = True
            break
        except Exception as error:  # Every item gets a state; nothing is retried.
            result = classify(error, mutation=True)
            code = "local_api_unavailable" if isinstance(error, NetworkError) else result.code
            if result.retryable and result.outcome != "unknown":
                item["code"], paused = code, True
                break
            item.update(state="uncertain" if result.outcome == "unknown" else "failed", code=code)
            if code in _QUEUE_STOPS:
                break
        else:
            item.update(task_id=task.get("id"), state=task.get("state"))
    counts = {name: sum(item["state"] == name for item in items) for name in ("failed", "uncertain", "not_started")}
    counts = {"submitted": len(items) - sum(counts.values()), **counts}
    if args.json:
        print_json({"items": items, "summary": counts})
    else:
        print_table(("#", "REFERENCE", "KEY", "TASK_ID", "STATE"),
                    [(item["position"], item["reference"], item["key"], item["task_id"],
                      item["state"].replace("_", " ") + (f" ({item['code']})" if item["code"] else ""))
                     for item in items], right=(0,))
        print(f"Submitted {counts['submitted']}, failed {counts['failed']}, uncertain {counts['uncertain']},"
              f" not started {counts['not_started']}.")
    if counts["uncertain"] or counts["not_started"]:
        notice(args, _REPLAY)
    if cancelled:
        return 130
    if counts["failed"] or counts["uncertain"]:
        return 4
    return 3 if paused else 0


def run_jobs(args) -> int:
    """Return the exit status: 0, or for watch 0, 3 or 4 (docs/cli-json.md)."""
    ensure_settings(args)
    try:
        return _run(args)
    except NetworkError as error:
        outcome, hint = _UNAVAILABLE.get(args.job_action, ("not_applied", _READ_HINT))
        raise CommandError(str(error), code="local_api_unavailable", name=type(error).__name__, outcome=outcome,
                           reason=getattr(error, "reason", None),
                           hint=hint.format(task_id=getattr(args, "task_id", ""))) from None


def _run(args) -> int:
    if args.job_action == "watch":
        task_id(args.task_id)
        if type(args.duration) not in (int, float) or not 1 <= args.duration <= 3600 or not math.isfinite(args.duration):
            raise ValueError("Choose a total observation duration between 1 and 3600 seconds.")
        if args.after is not None:
            validate_event_id(args.after)
    if args.job_action == "add":
        validate_idempotency_key(args.key)
    if args.job_action == "list":
        validate_task_page(args.limit, args.offset)
        if args.offset > 10000:
            raise ValueError("The HTTP API accepts offsets up to 10000.")
    api = LocalAPI(args.api_url, args.token_file or state_directory() / "api-token", timeout=args.timeout)
    if args.job_action == "watch":
        state = _watch(api, args)
        if state == "succeeded":
            return 0
        if state is not None:
            if not args.json:
                print(f"The task ended in state {state}.", file=sys.stderr, flush=True)
            return 4
        if not args.json:
            print("Stopped watching before the task finished. Run the same command to keep watching.",
                  file=sys.stderr, flush=True)
        return 3
    path = "/downloads"
    if args.job_action == "add":
        value = api.request("POST", path, body={"reference": args.reference, "idempotency_key": args.key})
    elif args.job_action == "list":
        value = api.request("GET", path, query={"limit": args.limit, "offset": args.offset})
    elif args.job_action == "resume":
        value = api.request("POST", path + "/resume")
    elif args.job_action == "file":
        value = {"path": str(api.file(args.task_id, default_output(args)))}
    else:
        path += "/" + args.task_id
        value = api.request("POST", path + "/cancel") if args.job_action == "cancel" else api.request("GET", path)
    if args.json:
        print_json(value)
    elif "tasks" in value:
        print_table(("TASK_ID", "STATE", "REFERENCE"),
                    [(item["id"], item["state"], item["reference"]) for item in value["tasks"]])
        print(f"{value['total']} task(s) in total.")
        if value.get("paused"):
            print("Queue paused by quota or rate limit. Use jobs resume when the allowance is available.")
    elif "id" in value:
        print(f"{value['id']}  {value['state']}  {value['reference']}")
    elif "resumed" in value:
        print(f"Queue resumed; {value['resumed']} task(s) requeued after quota wait.")
    else:
        print(value["path"])
    return 0


def _watch(api, args):
    """Return the terminal state observed, or None when the observation time ended first."""
    deadline = time.monotonic() + args.duration
    after = args.after
    first_stream = True
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (remaining < 1 and not first_stream):
            return None
        first_stream = False
        # The extra deadline caps all streams together, including connection
        # setup and a server that exceeds its requested observation duration.
        with closing(api._events(args.task_id, after=after, duration=max(1, min(30, remaining)), stop_at=deadline)) as stream:
            try:
                for event in stream:
                    after = event["id"]
                    if args.json:
                        print_json(event)
                    else:
                        task = event["task"]
                        line = f"{task['id']}  {task['state']:<14}  {task['reference']}"
                        progress = task["progress"]
                        if progress is not None:
                            total = progress["total_bytes"] if progress["total_bytes"] is not None else "?"
                            line += f"  {progress['bytes_received']}/{total} bytes"
                            if progress["bytes_per_second"] is not None:
                                line += f"  {progress['bytes_per_second']:.1f} B/s"
                        if event["paused"]:
                            line += "  queue paused"
                        print(line, flush=True)
                    if event["task"]["state"] in TERMINAL_STATES:
                        return event["task"]["state"]
            except NetworkError as error:
                if error.reason == "timeout" and time.monotonic() >= deadline:
                    return None  # Only the caller's explicit observation budget ended.
                raise
        # Only an explicit duration end can reach here without a terminal
        # observation. EOF, shutdown and protocol failures raise instead.
