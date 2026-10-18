# Pi Durable runtime for Cuse

A fresh Cloudflare Worker plus SQLite Durable Object implementation. It uses
Pi Durable 1.2.0 directly, with the canonical Python controller plans as its
control protocol. It does not use an existing Cuse runtime or the reference
app's unauthenticated chat routes.

## Implemented scope

- `PiHarness`: durable owner/configuration record, explicit model selection and
  an existing secret binding checked locally. `ready: true`, `phase: Configured`
  means this configuration is initialized. It does **not** mean that the model
  API, IRC server, WeeChat or MCP endpoint was contacted or verified.
- `Session`: its own SQLite Durable Object and exactly one Pi root conversation.
  `ready: true`, `phase: Ready` requires a real `Harness.open`, durable root
  initialization, matching model configuration and a ready parent PiHarness at
  the exact dependency UID/revision. This is runtime initialization, not proof of
  successful paid inference or completion of the full IRC/MCP workflow.
- Durable prompt admission and deduplication, persisted transcript, restart
  recovery, and a recovery alarm persisted **before** prompt admission. Active
  work is resumed after reopen. Idle rooms remove the alarm and use zero
  document-retention timers, allowing scale-to-zero.
- Existing canonical model/CRD fields are unchanged. `context_ref`, `share_url`
  and MCP endpoint references are metadata only. They are never fetched,
  imported, granted access, or treated as credentials. There is no public share
  endpoint or unauthenticated session route.

Tools are always `[]`. There is no shell, MCP tool execution, IRC ingest/reply
bridge, cross-session conversation creation, UI, WebSocket endpoint or public
sharing in this first implementation. The MCP/WeeChat workflow remains separate
integration work. User interfaces and per-user authorization need explicit
design before exposing this service to end users.

## Explicit deployment configuration

Nothing is deployed by installing or testing this directory. Production use
requires an approved Cloudflare deployment and the following existing bindings:

| Binding | Meaning |
| --- | --- |
| `RESOURCES` | SQLite Durable Object namespace for exported `PiResource` |
| `CONTROL_API_TOKEN` | Existing service bearer secret, 32–8192 printable ASCII characters |
| `PI_MODEL_PROVIDER` | Explicitly `openai`; other providers fail closed |
| `PI_MODEL_ID` | Explicit supported chat-model ID from the pinned Pi AI catalog |
| `PI_MODEL_SECRET_BINDING` | Name of an existing secret binding beginning `PI_MODEL_` |
| Named model secret | Existing provider API key value, kept in Worker secrets |

There is no default model or API key. The metadata/config variable names cannot
be used as secret names. The control token cannot be selected as a model key.
Only the named key is made available to the OpenAI provider; there is no ambient
process credential lookup, OAuth login, filesystem credential lookup, dynamic
catalog fetch, or provider/base-URL override. Configuration checks do not call
OpenAI. An authenticated input request **will** run model inference and can
incur provider charges once real credentials are installed.

These settings apply globally to this Worker deployment. A non-secret runtime
fingerprint is returned in observations. An idle root is reconfigured after a
configuration change. A root with unfinished work is blocked on a changed
fingerprint; restore the prior settings or perform a separately designed and
authorized migration rather than silently resuming it with a different model.
API key rotation at the same binding does not change that fingerprint.

All routes use the service bearer token. It is an administrative service
credential with access to every resource in this deployment, **not** a
per-user token. Put this private control service behind an appropriately
restricted network/auth boundary before production use. Separate browser/user
identities, rate limits, spending limits and credential provisioning are not
implemented or authorized by this code addition.

## Control API v1

Every request requires `Authorization: Bearer <existing CONTROL_API_TOKEN>`.
Bodies must be JSON. Control bodies are limited to 65,536 bytes. Responses are
`Cache-Control: no-store`; errors are generic codes and do not echo submitted
content, provider errors or credentials.

`GET /v1/resources/{PiHarness|Session}/{namespace}/{name}/{uid}`

- 404: `{ "exists": false, "revision": "", "ready": false, "phase": "Absent" }`
- 200: `{ "exists": true, "revision": "<64hex>", "ready": true|false,
  "phase": "Configured|Ready|DependencyNotReady|RuntimeBlocked",
  "runtime_revision": "<non-secret config fingerprint, when available>",
  "conversation_id": 1 /* only for ready Session */ }`

`PUT` at the same URL accepts exactly:

```json
{
  "identity": { "kind": "PiHarness", "namespace": "default", "name": "harness", "uid": "resource-uid" },
  "intent": { "kind": "PiHarness", "spec": {}, "userBinding": "...", "dependencies": [] },
  "revision": "<SHA256 of canonical controller intent>",
  "idempotency_key": "<SHA256 of [kind,namespace,name,uid,revision]>",
  "expected_revision": null
}
```

The `intent` above is abbreviated: send the actual controller-built canonical
intent. `test/python-plans.json` contains complete real Python controller
examples. Hashing exactly matches Python `json.dumps(sort_keys=True,
separators=(',', ':'), ensure_ascii=True, allow_nan=False)`, including Unicode
surrogate escaping and code-point key order. All accepted intent values are
objects, lists and strings, avoiding cross-language numeric rendering drift.

First creation requires `expected_revision: null`; updates require the exact
observed current revision. Successful writes return the observation (201 for
first write, 200 thereafter). Receipt replay with a stale expected revision returns the **current**
observation without rolling back newer intent. An explicit PUT with the exact
current expected revision can reassert an older intent (A → B → A); its
deterministic key is reused. This is content-hash compare-and-swap, not a
monotonic generation/version token: repeated ABA cycles are not distinguished.
The operation's identity/intent/revision/key cannot be changed without changing
the key. Wrong digests fail before writing.

- 400: invalid path/JSON or unsupported query
- 401: missing/incorrect bearer
- 405: unsupported method; there is no DELETE operation
- 409: ownership, revision or idempotency conflict
- 413/415/422: body too large, wrong content type, invalid intent/digest/model
- 503: missing credentials/configuration or transient runtime failure

A missing parent can leave a newly accepted Session at
`DependencyNotReady`; it is not falsely marked ready. Every Session observation
checks its parent UID and exact desired revision. Kubernetes still owns the
transitive IRC/WeeChat/MCP dependency readiness checks.

A Durable Object is addressed by kind plus Kubernetes UID; the complete
namespace/name/UID identity is persisted and checked. A UID cannot be reused
under a different name or namespace. Domain ID and PiHarness user owner are
immutable within that UID. A Session cannot switch its parent harness UID,
IRC server UID or channel. Such a change requires a new canonical Session UID;
old data remains retained. Deleting a Kubernetes object does not delete the
Durable Object, conversation, prompt receipts or controller receipts. Retention
cleanup is deliberately not provided.

## Authenticated session data API

- `POST <Session control URL>/input` accepts exactly
  `{ "request_id": "caller-stable-id", "content": "user message" }`.
  Text is limited to 16,000 characters and the body to 20,000 bytes. IDs are
  1–128 ASCII letters/digits with `.`, `_`, `:`, `-` after the first character.
  Reusing an ID with different content is a 409. Identical retries reuse the
  same durable Pi submission. Busy rooms queue follow-up inputs.
  A 202 response contains `submission_id` and `conversation_id`; it confirms
  durable admission, **not** inference completion.
- `GET <Session control URL>/snapshot` returns up to the newest 100 transcript
  entries, root ID, pending submission IDs/status, task counts and the underlying
  next-page cursor. Pagination transport is not yet exposed in this bounded
  initial API; older entries remain durably retained.

These endpoints require the same administrative bearer. `share_url` has no
relationship to permission to call them. No URL query tokens are accepted.
Prompt admission is local-idempotent; recovery may repeat an interrupted model
provider request. Exactly-once provider billing is not promised.

## Local verification

```sh
npm ci --ignore-scripts
npm run typecheck
npm test
npm run bundle
```

`bundle` is only Wrangler's `deploy --dry-run`, not a live deployment. Tests run
inside local workerd using SQLite Durable Objects. The test Worker blocks
external outbound requests, and task-recovery tests use Pi's offline faux
provider. Test-only model/key strings do not select production defaults or
provision credentials. `test/python-plans.json` was generated from the real
Python controllers and repository examples.

On a sandbox with an unwritable home directory, set `XDG_CONFIG_HOME` and
`NPM_CONFIG_CACHE` to writable temporary paths, and set
`WRANGLER_SEND_METRICS=false` for local checks.

See `THIRD_PARTY_NOTICES.md` for the pinned reference and retained MIT notice.
