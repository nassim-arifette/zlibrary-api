# Download tasks

Tasks let a running server download books while clients submit work, poll progress
or cancel a transfer. They use the same validated streaming download as
`Client.download()`. The task registry tracks this work; the separate
`GET /v1/history` route reads the download history recorded by Z-Library.

## Enable tasks

Create a download directory, then start an authenticated server:

```sh
zlib-cli serve --download-dir ./downloads --max-download-tasks 1000
```

The directory must already exist. Download tasks are disabled unless you pass
`--download-dir`; they cannot be enabled with `--anonymous`. The server binds its
port and verifies the account before starting the worker. It uses one worker and
serializes all upstream client operations. Task polling, cancellation and file
retrieval do not wait for that lock.

Each task gets its own directory. The server accepts book references, never
client-supplied output paths. Store the root in a directory you control.

## Submit and inspect

Every request needs the local server's bearer token, as described in the
[HTTP guide](http-api.md). Submit JSON with `Content-Type: application/json`:

```http
POST /v1/downloads

{"reference":"123:abc","idempotency_key":"reading-list-001"}
```

Replace the synthetic reference above with a real `ID:HASH` or a book URL on the
server's selected domain. The response is HTTP 202 and a task object. Its `id`
is a 32-character lowercase hexadecimal identifier. Use that ID with
`GET /v1/downloads/{task_id}` or `zlib-cli jobs show TASK_ID --json`.

From the command line, `zlib-cli jobs add ID:HASH --key KEY` submits a book;
`--key` is required and is the request key described below. `jobs list` prints
each task's ID, state and reference, and `jobs cancel` with that ID requests
cancellation. `jobs` uses `http://127.0.0.1:8000/v1` and the default token file;
for another server, put its options before the action, as in
`zlib-cli jobs --api-url http://127.0.0.1:8080/v1 --token-file PATH list`.

The request key must contain 8 to 128 ASCII letters, digits, dots, underscores,
tildes or hyphens. The server persists it before acknowledging the request or
contacting Z-Library. Reusing it with the same normalized reference returns the
existing task; using it for a different reference returns 409. Keys are never
automatically expired. Choosing a new key creates a new request, even for a book
that was already downloaded.

`GET /v1/downloads?limit=50&offset=0` returns `{tasks, total, limit, offset, paused}` in
creation order. HTTP limits are 1 to 1000 and offsets are 0 to 10000. Each task
includes `reference`, `state`, creation/update timestamps, `cancel_requested`,
`progress`, `file_available`, `error_code`, `error_message` and `outcome`.
Progress reports received bytes, total size when known, elapsed time, average
speed and estimated time remaining. Reaching the total byte count does not
confirm file validation or publication; wait for `succeeded`. The list's `paused`
flag tells you whether quota or a rate limit stopped dispatch.

## State, quota and cancellation

| State | Meaning |
| --- | --- |
| `queued` | Saved and waiting for the single worker. |
| `running` | The worker is checking quota or downloading. |
| `waiting_quota` | The service reported an exhausted allowance. Explicit resume is required. |
| `succeeded` | The validated file was published and its receipt saved. |
| `failed` | The attempt failed; inspect the safe error code. |
| `cancelled` | Cancellation completed before a final file was recorded. |
| `interrupted` | Shutdown or a prior process interruption prevented a confirmed result. |

Before each task, the worker checks quota. A confirmed zero pauses dispatch
without requesting a download link; absent counters remain unknown. A quota
refresh after a successful transfer cannot undo that success.

A rate-limit error during an attempt marks that task failed and pauses subsequent
work. This pause survives a restart, even if the queue was otherwise empty.
`resume` releases it without retrying the failed task. The refusal may reflect
request throttling or quota, so wait until the allowance is available.

Use `POST /v1/downloads/resume` or `zlib-cli jobs resume` once quota is available.
It returns `{"resumed": count}` and only requeues `waiting_quota` tasks. Releasing
a rate-limit pause with no such tasks returns zero. It does
not retry failed/interrupted tasks or resume partial bytes.

`POST /v1/downloads/{task_id}/cancel` records the request before signalling the
transfer. Repeated cancellation of a terminal task returns its existing state.
A file published before a late cancellation remains a success. Cancelling a
request already sent upstream may still consume account quota.

Stopping the server interrupts an active transfer and leaves unstarted tasks
queued. On the next explicitly enabled run, queued work continues; a task left
`running` by a crash becomes `interrupted` and is never replayed automatically.
A crash between file publication and recording success can leave a file on disk
without a confirmed task result. The server retains that file and does not
silently request another download. Storage errors stop further dispatch.

## Watch a task

```sh
zlib-cli jobs watch TASK_ID --duration 300
zlib-cli jobs watch TASK_ID --duration 300 --json
```

Watching does not start, cancel or retry a download. It stops when the task is
terminal, the requested duration expires, or you press Ctrl+C. The default is
60 seconds; `--duration` accepts 1 to 3600 seconds. JSON output is one complete
`TaskEvent` object per line, with `id`, `task` and `paused`. Ordinary output shows
the current state and available progress.

The HTTP route is `GET /v1/downloads/{task_id}/events?duration=30`. It requires
the same bearer token, Host and Origin checks as the other routes. A stream lasts
1 to 60 seconds and uses UTF-8 `text/event-stream`. Each `task` event carries a
complete public task object and the queue's pause flag:

```text
event: task
id: 0123456789abcdef0123456789abcdef:7
data: {"id":"0123456789abcdef0123456789abcdef:7","task":{...},"paused":false}

```

The cursor is an opaque process epoch and revision, not a credential. Supply the
last cursor in `Last-Event-ID` or the `after` query parameter, never both. An exact
match suppresses duplicate nonterminal state. An older cursor or a server restart
returns the latest full state. Terminal tasks are always reported once, even when
the cursor matches. You can also read `GET /v1/downloads/{task_id}` after any
interruption.

Updates can be coalesced when a consumer is slow. This feed reports current state;
it does not replay every transition or preserve an event history. An idle stream
sends a comment heartbeat at least every five seconds. Each frame is bounded to
64 KiB. A final `end` event names its reason: `terminal`, `duration` or `shutdown`.
An unexpected disconnect is an error and does not imply that the task finished.

The CLI reconnects after a normal duration boundary, passing its latest cursor,
until its total observation time expires. It does not retry network, protocol or
storage failures. Stopping the server while a watched task remains unfinished is
reported as an interrupted observation.

At most four HTTP streams are active at once, also limited to half the configured
connection slots (with one stream allowed when only one slot is configured).
Excess streams receive 503. A slow reader cannot hold the worker lock or prevent
another client from cancelling a task. Subscriptions are released on disconnect,
timeout or shutdown.

For a browser, use a streaming `fetch()` request with an Authorization header and
an explicitly allowed origin. Native `EventSource` cannot set that header; bearer
tokens are never accepted in a URL. Frame syntax follows the
[HTML server-sent events standard](https://html.spec.whatwg.org/multipage/server-sent-events.html).

## Retrieve a completed book

`GET /v1/downloads/{task_id}/file` returns the validated PDF, EPUB or DjVu as an
attachment. The server reopens only the recorded file, checking its identity,
size and timestamps. It rejects replacement files, links and paths outside the
task directory. File responses use bounded chunks and a 30-second send deadline.

These checks use filesystem metadata, not a content checksum. They do not prove
that a file was never altered by someone able to restore its metadata. On Windows,
Python versions can report different device identifiers and timestamp meanings.
After a runtime upgrade, an existing receipt may no longer match: retrieval then
returns 410, retaining the original file on disk without downloading it again.

`zlib-cli jobs file TASK_ID --output .` makes a validated copy with a new filename
without overwriting existing files or consuming Z-Library quota again. The CLI
uses a loopback HTTP address and an existing private token file. It follows no
redirects, ignores proxy settings and never starts a background server.

## Use tasks directly from Python

```python
from pathlib import Path
from zlib_connector import Client, DownloadManager, SessionStore

with Client("https://z-lib.by/") as client:
    client.restore_session(SessionStore())
    with DownloadManager(client, Path("downloads")) as downloads:
        task = downloads.submit("ID:HASH", idempotency_key="reading-list-001")
        print(downloads.get(task.id).to_dict())
        # Keep this context open while your application polls or handles input.
        # Leaving it stops and joins the worker before Client closes.
```

`list(limit=50, offset=0)` returns `TaskPage`; `get`, `submit` and `cancel` return
immutable `DownloadTask` records. `open_file(id)` is a context manager yielding
`DownloadFile(stream, name, content_type, size)` for a succeeded task. Errors are
exported from `zlib_connector.jobs`. If other code shares the synchronous Client,
pass one `threading.Lock` as `client_lock` and use it for every other Client call.

Python consumers can subscribe to the same observations without starting HTTP:

```python
with downloads.watch(task.id) as observations:
    while not observations.closed:
        event = observations.next(timeout=5)
        if event is not None:
            print(event.to_dict())
```

`watch(task_id, after=None)` returns a context-managed subscription. `next()`
returns an immutable `TaskEvent` or `None` when its wait expires. Close a
subscription when leaving the view; closing it never cancels the task. A manager
permits up to 32 subscriptions and wakes them on shutdown. Observation reads do
not contact Z-Library or write the task journal. These are the shared Python
observations available for future TUI views.

## Storage and current limits

Private metadata lives under `.zlib-jobs/<account-scope>/tasks.json` in the root.
The scope is an HMAC of the exact domain and account ID using a private local key;
the journal stores neither raw account IDs nor authentication cookies. Keep the
root's `.zlib-jobs/scope-key`: a missing key with existing journals stops startup.
Only one manager can own an account journal at a time. The file is replaced
atomically; progress is persisted at state transitions and overlaid in memory
during a transfer.

The registry defaults to 1000 tasks per account and permits 1 to 10000, with a
32 MiB journal limit. It rejects new requests when full while keeping existing
idempotency keys usable. There is no automatic eviction, deletion command, retry,
whole-queue event feed, byte-range resume or import from booklists yet. Completed files are
not removed by cancellation or server shutdown.
