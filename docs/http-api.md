# HTTP API

Start with `zlib-cli serve`, or `zlib-cli serve --anonymous` for public browsing.
The default address is `http://127.0.0.1:8000/v1`. Global `--domain`,
`--credentials` and `--timeout` options go before `serve`.

The server signs in once and keeps one account session in memory. It binds the
local port before asking for credentials or logging in. All client operations
are serialized, including the read/write sequence of a booklist edit. Stopping
the server closes its workers before discarding the session. In-flight requests
may need to finish before shutdown completes.

## Authorization

Every API request requires `Authorization: Bearer TOKEN`. There is no token in
the URL and no endpoint that accepts a Z-Library password. `serve` reads or
creates the bearer file atomically and prints only its path.

The default file is `%LOCALAPPDATA%/zlibrary-api/api-token` on Windows and
`$XDG_STATE_HOME/zlibrary-api/api-token` on Linux, falling back to
`~/.local/state/zlibrary-api/api-token`. `ZLIB_STATE_DIR` overrides the directory
with an absolute path; `serve --token-file PATH` overrides the file. The file
must contain 32–256 URL-safe ASCII letters, digits, underscores or hyphens.
New files have mode 0600 on POSIX. On Windows, their permissions grant access to
the current user and SYSTEM without inheriting broader directory permissions.
Existing files must belong to a trusted owner and exclude access by other users;
Windows also permits built-in Administrators. Unsupported or complex ACLs are
refused. Store tokens in a directory you control. Keep this file private:
its token grants access to the account operations of the running server.

The server supports only IPv4 loopback addresses and `localhost`. It validates
the Host header against its listening address and port. Browser Origin headers
are rejected by default. Repeat `--allow-origin ORIGIN` to allow specific HTTP
or HTTPS origins, without paths or wildcards. Allowed CORS preflights return
only permitted methods and headers and never access the account; actual requests
still need the bearer token.

## Routes

All routes are under `/v1`. Search and collection pages return
`{books, pagination}`. Missing upstream totals remain null. Book references in
paths contain the numeric book ID and alphanumeric hash from a search result.

| Method and route | Purpose |
| --- | --- |
| `GET /health` | Local process health; does not contact Z-Library. |
| `GET /capabilities` | Implemented operations and unavailable capabilities. |
| `GET /openapi.json` | OpenAPI 3.1 document with input and result schemas. |
| `GET /search?q=...` | Search; page, limit, repeated language/format, years and order. |
| `GET /books/{id}/{hash}` | Book details. |
| `GET /books/{id}/{hash}/formats` | Alternative formats and references. |
| `GET /books/{id}/{hash}/similar` | Similar books. |
| `GET /profile`, `GET /quota` | Account quota projection without raw profile fields. |
| `GET /domains` | Known candidates without a network probe. |
| `POST /domains/diagnostics` | Anonymous probes with optional domains, timeout and workers. |
| `GET /library?limit=5` | Separate previews of favorites, account history and booklists. |
| `GET /history?page=1&limit=20` | Downloads recorded by Z-Library for the signed-in account. |
| `GET /favorites` | Paginated favorites. |
| `PUT /favorites/{id}/{hash}` | Save a favorite. |
| `DELETE /favorites/{id}/{hash}` | Remove a favorite. |
| `GET /my-library/tags` | Native tag names. |
| `GET /my-library/saved-ids` | Native saved-book identifiers. |
| `PUT /my-library/books/{id}/{hash}/tags` | Add tags from `{"tags":[...]}`; also saves the favorite. |
| `DELETE /my-library/books/{id}/{hash}/tags` | Remove the supplied tags; keep the favorite. |
| `GET /booklists` | Personal booklists. |
| `POST /booklists` | Create a list from title and optional description, is_public, color; returns 201. |
| `PATCH /booklists/{id}` | Edit supplied settings, preserving the others. |
| `DELETE /booklists/{id}` | Permanently delete the remote list. |
| `GET /booklists/{id}/books?page=1` | Booklist entries, including their membership IDs. |
| `POST /booklists/{id}/books` | Add `{"reference":"ID:HASH"}`. |
| `DELETE /booklists/{id}/entries/{entry_id}` | Remove a membership entry, not a catalog book ID. |
| `GET /discovery/popular`, `GET /discovery/recommended` | Popular titles and account recommendations. |

The history route reads **Z-Library's account download history**. It does not
scan this computer or claim that downloaded files are still present. Its
optional `order` is sent upstream; the service may ignore it. `page` is 1–1000
and `limit` is 1–50. Favorites and search use the same page bounds.

Routes with a JSON body require `Content-Type: application/json` and a JSON
object. Unknown fields, duplicate fields, invalid combinations and unsupported
query parameters are rejected. Bodyless mutations do not accept JSON bodies.
Account writes are explicit HTTP operations and take effect without an
interactive CLI confirmation. New booklists are private unless `is_public` is
true. Tag additions report `adds_to_favorites: true`. Edits have the upstream
concurrency limitations described in the [reference](reference.md).

The API currently has no HTTP login/logout, download jobs or file retrieval,
upload routes, local download database, or full native My Library pages.
The Python client and CLI continue to support downloads. Anonymous mode exposes
the same routes, but account operations return an authentication error.

## Errors and limits

Errors use a consistent JSON object:

```json
{"error":{"code":"mutation_uncertain","message":"The server may have applied this change. Check the account before trying again.","retryable":false,"outcome":"unknown"}}
```

An uncertain mutation returns HTTP 409. Read the account state before deciding
whether to repeat it. The server never automatically retries an account write.
`retryable` is advisory for callers; it does not schedule another attempt.
Raw request values, upstream error bodies, account secrets and stack traces are
not returned or logged.

Other responses include 400 for invalid input, 401 for a missing/invalid local
bearer, 403 for a rejected Host or Origin, 404/405 for unknown routes/methods,
404 `booklist_not_found` when an editable list is absent (no write was attempted),
424 for a missing/expired upstream account session, 429 for an upstream limit,
502/504 for upstream failures, and 503 when the local server or client is busy.

Requests are bounded to an 8 KiB request line, 4 KiB query, 16 KiB headers,
64 header fields and 64 KiB JSON body. The default read deadline is five seconds,
with at most eight concurrent connections. There is one request per connection;
chunked/compressed request bodies are not accepted. Upstream `--timeout` is
separate from the local read deadline. Domain diagnostics permit up to 50
candidates, 1–8 workers and 0.1–30 seconds per upstream request.

The `/v1` prefix identifies the current HTTP contract. Future incompatible route
or field changes require a new API version. Clients should tolerate additional
response fields; request fields are deliberately validated strictly. The running
server's OpenAPI document is the reference for its implemented operations.

## Embedding in Python

```python
from zlib_connector.http_api import create_server

# client is caller-owned and may already be signed in; token comes from private storage.
with create_server(client, token=token, port=8000) as server:
    server.serve_forever()
```

The server does not close the injected client. Stop and close the server before
closing that client. If serving on another thread, call `shutdown()` from the
controlling thread, then `server_close()`. `openapi_document()` returns the same
contract as the authenticated HTTP route without starting a server.

Tests use synthetic clients and real loopback connections. They verify routing,
pagination, account updates, authorization, CORS, parsing limits, redacted
errors, uncertain mutations and serialized client access. No account writes are
performed to test this HTTP adapter.
