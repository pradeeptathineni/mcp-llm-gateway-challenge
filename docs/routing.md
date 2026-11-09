# Token budgets and model fallback

The LLM gateway serves `/v1/completions` alongside `/v1/stream`. Both authenticate with the same server-controlled token map and share on-disk token admission. With the mock provider and gateway running:

```sh
curl http://127.0.0.1:8012/v1/completions \
  -H 'Authorization: Bearer demo-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"primary-rate-limit","max_tokens":256}'
```

Use `demo` for primary success, `primary-rate-limit` for a mocked 429, and `primary-timeout` for the three-second fallback demonstration. These prompts control only the local fixture provider.

## Admission and accounting

Each tenant has a sliding budget of 50,000 tokens per 60 seconds. A short SQLite `BEGIN IMMEDIATE` transaction evicts expired reservations, sums the tenant's active tokens, and admits the next request only if it fits. The interval is `(now - 60, now]`. Independent worker processes sharing this file participate in the same serialized admission.

Before provider I/O, the gateway reserves UTF-8 prompt bytes, 32 framing tokens, and the requested maximum output tokens. This is a conservative assessment adapter, not an exact tokenizer for every model. Production adapters should use the model's tokenizer and exact message framing. The underlying limiter accepts exact integer costs independently of this adapter.

A successful primary response with consistent reported usage settles the reservation once. Missing or inconsistent usage, cancellation, failures, streaming requests, and fallback retain the conservative reservation. Unknown-usage responses are accepted only if their output bytes fit the reserved output budget. Reported output usage must fit `max_tokens` and total usage must fit the reservation.

Fallback is one user request and therefore one tenant admission. The retained reservation is not a provider billing ledger: a timed-out primary may still incur cost, and total vendor charges can differ. No paid provider is used by the default configuration.

SQLite contains reservation IDs, hashed tenant identifiers, epoch timestamps, and token counts. It stores no bearer tokens, prompts, response text, or upstream errors. Epoch time survives restarts, and backward clock changes clamp conservatively rather than expiring usage early. SQLite WAL improves reader/writer concurrency but does not remove its single-writer limit.

## Failover and errors

`/v1/completions` wraps the entire primary request, including response-body consumption, in a 3000 ms async deadline. HTTP 429 or timeout closes/cancels that attempt before contacting the backup. The backup has its own bounded deadline. Other HTTP errors are not retried. No fallback text is spliced into a partial primary stream.

The streaming route has no model failover after output starts. It retains its budget and emits a safe error event if the provider fails. This avoids mixing two generations after the client has received text.

Public errors use one stable `error.code` and `error.message` shape. Quota exhaustion returns HTTP 429 and `Retry-After`. Database failures return HTTP 503, provider failures HTTP 502, and invalid requests HTTP 400. Raw upstream bodies, stack traces, credentials, and exception strings are never included.

## Configuration and lifecycle

- `GATEWAY_DATABASE`: local SQLite path, default `var/gateway.sqlite3`.
- `LLM_PRIMARY_URL`: trusted completion provider, default `http://127.0.0.1:8011/primary`.
- `LLM_BACKUP_URL`: trusted backup, default `http://127.0.0.1:8011/backup`.
- `LLM_STREAM_URL`: trusted SSE provider, default `http://127.0.0.1:8011/stream`.

These URLs are operator configuration, never caller input. Database files and sidecars are ignored by Git. Keep the file on local storage, not a network filesystem. A multi-host deployment needs a shared admission service rather than separate SQLite copies. The service binds to loopback in every documented command and fixture tokens must never protect an internet-facing deployment.

## Verification

`make check` covers exact 49,000 + 1,000 admission, rejection of the next token, tenant independence, exact expiry, backwards time, reopening the database, idempotent settlement, 80 concurrent calls, eight independent worker processes, 429 and timeout fallback, trickling-body deadlines, cancellation cleanup, failure sanitization, and HTTP quota responses. A real-socket test also verifies the default three-second deadline.

## Sources

- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [SQLite WAL](https://www.sqlite.org/wal.html)
- [HTTPX timeout semantics](https://www.python-httpx.org/advanced/timeouts/)
- [Python async timeout contexts](https://docs.python.org/3/library/asyncio-task.html#timeouts)
