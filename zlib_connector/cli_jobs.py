"""CLI operations on the download tasks of an existing local API server."""
import argparse
import json
from pathlib import Path

from .local_api import LocalAPI, task_id
from .local_state import state_directory
from .jobs.models import validate_idempotency_key, validate_task_page


def add_jobs_parser(commands):
    command = commands.add_parser("jobs", help="manage downloads on a running local API server")
    command.add_argument("--api-url", default="http://127.0.0.1:8000/v1", help="loopback HTTP API address")
    command.add_argument("--token-file", type=Path, help="existing server token file (default: state directory/api-token)")
    command.add_argument("--json", action="store_true")
    actions = command.add_subparsers(dest="job_action", required=True)
    for name in ("add", "list", "show", "cancel", "resume", "file"):
        action = actions.add_parser(name)
        action.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name in ("show", "cancel", "file"):
            action.add_argument("task_id", type=task_id)
        if name == "add":
            action.add_argument("reference", help="ID:HASH or a book URL on the server's selected domain")
            action.add_argument("--key", required=True, help="unique request key; reuse it only to replay the same submission")
        if name == "list":
            action.add_argument("--limit", type=int, default=50)
            action.add_argument("--offset", type=int, default=0)
        if name == "file":
            action.add_argument("--output", type=Path, default=Path.cwd(), help="existing directory for a validated local copy")


def run_jobs(args):
    if args.job_action == "add":
        validate_idempotency_key(args.key)
    if args.job_action == "list":
        validate_task_page(args.limit, args.offset)
        if args.offset > 10000:
            raise ValueError("The HTTP API accepts offsets up to 10000.")
    api = LocalAPI(args.api_url, args.token_file or state_directory() / "api-token", timeout=args.timeout)
    path = "/downloads"
    if args.job_action == "add":
        value = api.request("POST", path, body={"reference": args.reference, "idempotency_key": args.key})
    elif args.job_action == "list":
        value = api.request("GET", path, query={"limit": args.limit, "offset": args.offset})
    elif args.job_action == "resume":
        value = api.request("POST", path + "/resume")
    elif args.job_action == "file":
        value = {"path": str(api.file(args.task_id, args.output))}
    else:
        path += "/" + args.task_id
        value = api.request("POST", path + "/cancel") if args.job_action == "cancel" else api.request("GET", path)
    if args.json:
        print(json.dumps(value, ensure_ascii=False))
    elif "tasks" in value:
        for item in value["tasks"]:
            print(f"{item['id']}  {item['state']:<14}  {item['reference']}")
        print(f"{value['total']} task(s) in total.")
        if value.get("paused"):
            print("Queue paused by quota or rate limit. Use jobs resume when the allowance is available.")
    elif "id" in value:
        print(f"{value['id']}  {value['state']}  {value['reference']}")
    elif "resumed" in value:
        print(f"Queue resumed; {value['resumed']} task(s) requeued after quota wait.")
    else:
        print(value["path"])
