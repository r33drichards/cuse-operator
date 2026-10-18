# cuse-operator

A Python 3.12 **Kopf controller scaffold** for IRC, WeeChat, Pi harnesses,
sessions and session-scoped MCP clients. The canonical data model uses frozen,
slotted stdlib dataclasses. It is deliberately separate from Kubernetes resource
metadata and status.

**Implemented:** strict codecs, structural and semantic validation, independent namespaced Bot, Session, PiHarness, MCPClient, Connector, WeeChat
and shared IRCServer CRDs, reference validation and dependency watches, sanitized
status, dedicated desired-state controllers for all eight CRDs, explicit provider
interfaces, idempotent in-memory convergence tests, minimal RBAC and offline CLI. The original Architecture
aggregate remains supported unchanged.
**Concrete opt-in implementations:** retained Kubernetes workloads for IRCServer,
WeeChat and MCPClient; linked Connector and logical Bot bindings; authenticated
Cloudflare control client and a fresh Pi Durable Worker/SQLite runtime for
PiHarness and Session. [Runtime adapters](docs/runtime-adapters.md) describe
activation, readiness and the remaining integration gaps. Architecture remains
compatibility-only and never provisions a duplicate graph.

The default provider reports `ready=false` / `ProviderNotConfigured`.
`CUSE_PROVIDER_CONFIG` explicitly enables supported effects; it is not a dry-run
switch. WeeChat currently stays not-ready because upstream lacks safe live IRC
connection proof. IRC-to-Pi routing, MCP tools inside Pi, context imports,
end-user sharing/auth, admission webhooks and MCP Events remain unimplemented.
No cluster/cloud deployment or real inference has been tested or performed.

## Develop and verify

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check .
python -m pytest -q
python -m compileall -q src
cuse-validate examples/architecture.json
cuse-validate examples/resources.json
python -m cuse_operator.schema --all --output config/crds.json
```

The example is a bare domain Architecture; the CLI also accepts an Architecture
Kubernetes resource envelope with the domain object under spec.
`examples/resources.json` shows all seven independent resources in a Kubernetes List;
the CLI validates their reference graph without contacting a cluster. Validation is offline. No tests need cluster credentials.

## Read first

- [Current design and implementation plan](docs/design-and-plan.md)
- [Original gist design, retained as superseded history](docs/history/original-design-and-plan.md)
- [Canonical models](src/cuse_operator/models.py) and [validation policy](src/cuse_operator/validation.py)
- [Tracked work and acceptance criteria](https://github.com/r33drichards/cuse-operator/issues)

## Kubernetes boundary

`config/crds.json` contains all eight CRDs under provisional `cuse.io/v1alpha1`.
`config/crd.json` retains the original Architecture definition for compatibility.
The API group makes no domain-ownership claim: choose an owned group before release.
Split-resource specs use same-namespace name references with optional UID pins;
ChannelRef stays inline. See [the API mapping](docs/design-and-plan.md) for every field.
Existing Architecture resources never automatically create or overwrite split resources.
When upgrading an installation, install all definitions from config/crds.json and update
config/rbac.yaml before starting this version; the split watcher requires all seven kinds.

The scaffold can be run, **only in an explicitly approved test namespace**, with:

```sh
kopf run --standalone --namespace=default -m cuse_operator.operator
```

This command starts the eight controllers together and writes reconciliation status.
It also lists split resources and patches dependent statuses after changes/deletions.
It is not a no-write cluster dry-run. The offline validation helpers and CLI are the
offline dry-run surfaces. Installation and cluster execution are not performed by
this repository setup. Sample RBAC covers one namespace and one standalone operator;
no Secret, workload, cluster-admin, peering or external-provider privileges are granted.
See [the design](docs/design-and-plan.md) for API admission and runtime limitations.

## Security

Never place credentials, signed URLs, bearer-capability URLs, transcripts, cookies
or private operational data in domain URLs/storage references or Kubernetes specs.
The validator catches recognizable credential syntax, but cannot detect every secret
hidden in an opaque path or identifier. Operator status is generic and redacted;
Specs and deterministic desired-state revisions are not credential storage. Raw watches
do not write Kopf diff-base annotations; provider adapters must keep credentials separate.
No license has been selected; public visibility alone does not grant a software license.
