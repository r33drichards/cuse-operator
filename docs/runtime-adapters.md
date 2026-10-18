# Source-backed runtime adapters

This implementation is staged in a draft PR. It does not yet implement every
runtime. No cluster, Cloudflare account, external endpoint or credential has been
used by its tests. See [tracking issue #11](https://github.com/r33drichards/cuse-operator/issues/11).

## Implemented execution boundaries

- **IRCServer:** actual Kubernetes Deployment and internal ClusterIP Service
  reconciliation through injected `kubernetes_asyncio` clients. Explicitly opt-in.
  Detects managed-field drift, verifies CR UID ownership before writes, observes
  rollout and ready selected Pod/Endpoints, and retains resources on deletion.
- **Connector referencing WeeChat:** logical binding reconciliation. It observes
  the owning WeeChat provider's readiness; it never creates another IRC client.
  This is not a generic remote MCP endpoint health checker.
- **WeeChat / MCPClient:** actual retained ConfigMap, Deployment and Service
  convergence with explicit digest-pinned images, Secret projections and drift
  checks. Readiness also requires the bounded protocol adapter. WeeChat remains
  not-ready because pinned upstream has no safe current IRC connection proof.
- **PiHarness / Session:** HTTPS desired-state control client and a fresh Worker
  runtime using real Pi Durable SQLite storage, CAS updates, retained UID-scoped
  objects, root conversations, prompt deduplication and recovery alarms.
- **Bot:** logical readiness binding over WeeChat and PiHarness. No extra process
  or message routing is implied. **Architecture:** compatibility-only, always
  explicitly not-ready; it never expands an embedded graph into live resources.

`ProviderRouter` selects a separately configured adapter per kind. Before an
observation, the controller binds its current plan and transitive dependency
plans. This is synchronous, IO-free preparation: a restarted adapter can compare
live managed fields with current desired state without trusting a revision label
or a previous successful `ensure` in process memory.

## IRCServer opt-in configuration

The user selected [r33drichards/docker](https://github.com/r33drichards/docker/tree/92cd2c5884d3b695c59de5325b7e3ce1e07838fb),
not the upstream image. That fork's checked workflow only publishes when run in
`inspircd/docker`. No matching fork image is assumed. Supply a verified fork image
pinned by digest; building/publishing an image is a separate explicit operation.

`CUSE_PROVIDER_CONFIG` can point to a local JSON file. Omitting it preserves the
status-only default. The file is operator configuration, not a CRD schema or
credential store. Version 1 requires `version` and `ircservers`; optional `weechats`, `mcpclients`,
`readiness` and `durable` sections configure the other adapters:

```json
{
  "version": 1,
  "ircservers": [{
    "namespace": "example",
    "name": "irc",
    "image": "YOUR-VERIFIED-FORK-IMAGE@sha256:YOUR-VERIFIED-DIGEST",
    "service_name": "irc",
    "cluster_domain": "cluster.local",
    "network_name": "ExampleNet",
    "endpoint_mode": "internal-plaintext"
  }]
}
```

The image above is an intentionally invalid placeholder, not a deployable image.
`namespace` and `name` select one IRCServer CR; `service_name` also names its retained
Deployment. Its spec endpoint must exactly be
`irc://irc.example.svc.cluster.local:6667` for those example settings. Generic
canonical IRC endpoints remain valid data, but this concrete provider rejects
unsupported endpoints instead of silently changing DNS, TLS or routing.

The adapter creates one replica with Recreate strategy, the verified source
entrypoint, UID/GID 10000, no service-account token mount, TCP startup/readiness
probes and no operator privileges. It disables the image's DNSBL lookup to avoid
an implicit external dependency. It exposes only TCP 6667 through the Service.
It does not provide authentication, trusted TLS, a NetworkPolicy, persistent
configuration, IRC registration checks or high availability. Internal Service
means cluster-routed, not private from every workload. The source image can still
listen on its other built-in ports and generates its own temporary TLS files;
this provider never claims those are trusted or publishes those ports.

The default Role is unchanged. `config/rbac-irc-provider.yaml` is an optional,
separate reviewable Role for the concrete adapter. It grants get/create/patch on
Deployments and Services, and get on Pods and Endpoints. It has no binding and is
not applied automatically. Running with this additional access requires separate
operational approval. Installing a Role is not performed by loading config.

Ownership is a full kind/namespace/name/UID annotation plus a UID-derived selector.
Existing objects without matching ownership, with ownerReferences, or terminating
are rejected. This is collision prevention, not a security boundary against an
actor who can rewrite annotations. JSON patches test object UID and resourceVersion.
Conflicts retry by fresh reads. Pod-only drift makes readiness false but does not
trigger Pod deletion or an automatic rollout; recovery of that case requires an
explicit external rollout. Unknown write outcomes are re-observed. Shared
references never become ownerReferences. CR deletion retains the workload and
Service, and a new CR UID cannot silently adopt them.

## WeeChat / Connector linkage (reviewable additive API)

`Connector.spec.weechatRef` is optional and uses the same `{name, uid?}`,
same-namespace reference contract as existing links. Existing Connector specs
and all canonical dataclasses are unchanged. When supplied, the referenced
WeeChat must have the same server, nickname and MCP endpoint. Missing/deleting
owners block; changed UID pins or mismatched identities invalidate the binding.

The proposed runtime ownership is: one WeeChat CR owns one bundled weechat-mcp
runtime; its Connector exports that runtime's MCP endpoint downstream to mcp-js.
A Connector does not launch a duplicate WeeChat. Deleting either CR does not
cascade to the other. A generic Connector without this reference needs its own
explicit provider and does not become ready through this binding adapter.

## mcp-js contract and authentication

Inspected [mcp-js 8d1d4f9](https://github.com/r33drichards/mcp-js/tree/8d1d4f9d568e395b8e3f8dfe5cb17b8ef58ff43b)
and [weechat-mcp 7d93473](https://github.com/r33drichards/weechat-mcp/tree/7d934730d6b6864a937035201f6caa039b080792).
The MCPClient controller resolves Connector IDs and complete MCP URLs into
`downstreams`; the renderer generates the real `mcp_servers` array with
`transport: http`, and explicit policy/host settings and launch args. Inline policy inputs are emitted
as separate mounted `.rego` artifacts (`policy_files`) with supported `file://`
references in the generated config; their contents are included in the rollout
digest. Mounting only the JSON file is insufficient. It never
appends `/mcp` or interprets the advertised endpoint as an ingress instruction.

Host restrictions are also passed as CLI arguments because the mcp-js image sets
`MCP_V8_ALLOWED_HOSTS=*` and environment values override file values. Explicit
inline MCP tool policies are mandatory: the inspected runtime skips its tool
policy gate when no chain exists. `deny_all_policies()` is an available explicit
choice; nothing silently grants tools. Policy syntax and semantics still need
verification in the real mcp-js runtime. No authenticated downstream probe runs
while rendering. Config changes require process rollout; no hot-reload API was
found. Even a listening HTTP port does not prove downstream tool availability.

The inspected mcp-js supports OAuth-browser downstream auth but no static bearer
config; stock weechat-mcp HTTP requires a bearer token outside its guarded Railway
mode. The user has authorized adding secret-reference support to mcp-js. That
separate source change must land and its compatible image be selected before
this linkage can run. No fake header config, Railway environment bypass, inline
token, Secret read grant or credential creation is included here.

## Fresh Pi Durable implementation

The user selected [Pi Durable on Cloudflare](https://earendil.com/posts/pi-durable-on-cloudflare/)
as the reference, explicitly rejecting reuse of existing Cuse runtime. The implemented
[Worker runtime](../runtime/pi/README.md) exposes a bearer-authenticated control API.
Kopf uses `durable: {"endpoint": "https://YOUR-CONTROL-SERVICE", "token_file": "/mounted/control-token"}`.
The file is an existing operator mount; no Secret API reads or credentials are
created. Each Session maps to an isolated UID-scoped DO/root conversation; deletion
retains data. CAS/idempotency guard retries. All routes, including snapshots, need
authentication; `share_url` remains metadata and never becomes a public capability.

Runtime readiness means durable configuration/root initialization, not successful
model inference or an end-to-end IRC workflow. Explicit global model settings and
existing Worker secret bindings are required. Tools remain empty; IRC ingress,
MCP execution, context import, UI/WebSockets and per-user authorization are open.
See the runtime README for exact API, local verification and production limits.

## Workload and protocol activation

The optional `weechats` and `mcpclients` arrays select namespace/name and require
all fields of `WeeChatSettings` and `MCPClientSettings` respectively in
[`mcp_workload_provider.py`](../src/cuse_operator/mcp_workload_provider.py).
No configuration defaults select images, permissions or channels. Both currently
require explicitly ephemeral storage and an internal HTTP endpoint exactly matching
the configured Service. WeeChat bearer auth uses an existing `auth_secret` name/key;
MCPClient `secret_profiles` maps downstream IDs to existing Secret name/key pairs.
A compatible mcp-js bearer-file build and explicit `supports_bearer_file: true`
are required when profiles are used. ConfigMaps contain paths, never token values.
Secret environment changes need a rollout; projected bearer files are read by
mcp-js on connection/reconnection. Inbound mcp-js is explicitly unauthenticated
internal HTTP: ClusterIP is not a security isolation boundary.

`config/rbac-workload-providers.yaml` adds only namespace-local get/create/patch
on Deployments, ConfigMaps and Services, plus get on Pods/Endpoints. It is unbound
and never applied automatically; it grants no Secret reads, deletes or cloud access.

Protocol probes require a separate explicit endpoint allowlist, for example:

```json
{"readiness": [{"endpoint": "https://YOUR-MCP-ENDPOINT/mcp",
 "provider": "WeeChat", "auth_mode": "bearer", "token_file": "/mounted/probe-token"}]}
```

This is a fragment of the operator config, not a CRD. Internal plaintext requires
explicit `allow_cluster_plaintext: true` and `cluster_domain`, and only approved
service DNS endpoints are accepted. Probe credentials must be separately mounted
in the operator; workload Secret references do not grant operator access. Probes
perform initialize/initialized/tools-list only, with bounded JSON/SSE, no redirects,
proxy inheritance, tool calls or prompts. Cleanup may DELETE only their own newly
created MCP protocol session. MCPClient success proves inbound protocol readiness,
not execution of every downstream tool. WeeChat protocol success still returns
`irc-readiness-unsupported`; no historical buffer data is used as live proof.

[`examples/provider-config.json`](../examples/provider-config.json) is a complete
parser-checked illustration, deliberately **nondeployable**: example.invalid images,
fake digests, placeholder endpoints and illustrative Secret references. It selects
the names used by `examples/resources.json`, but that older structural example's
URLs/nickname and optional Connector.weechatRef must be reconciled with the bundled
runtime contract before activation. Do not apply these samples as production settings.

## Remaining implementation path

1. IRC ingress/reply adapter: define the authenticated ordered-event transport,
   bind server/channel/session and sender identity, persist event cursors, deduplicate
   input IDs/replies, and exercise disconnect/replay/backpressure with fixtures.
   Historical buffer polling is not reliable ingestion. Experimental MCP Events
   remains tracked separately in issue #8.
2. Pi MCP tool adapters: explicit per-tool allowlists and secret-binding references,
   stable durable tool-call IDs, uncertain-outcome handling and side-effect tests
   before any real calls. The current runtime intentionally has an empty tool list.
3. Context import: select context_ref storage/resolver and format, ownership checks,
   immutable version/hash, size limits and SSRF controls; test retry/retention before
   enabling retrieval. Current references are metadata only.
4. User/share authorization: select the identity verifier and owner/grant model,
   enforce it on read/input/share with revocation/expiry and cross-user rejection
   tests. The current service bearer is administrative, not an end-user credential.
5. Build verified selected-source images and run approved artifact integration tests.
   Image publication, credentials, account access, live inference and deployment
   each remain separate operational prerequisites, not consequences of this PR.
