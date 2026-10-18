"""Pure, bounded mcp-js HTTP configuration rendering; no runtime reconciliation.

Contract inspected at MCP_JS_SOURCE_COMMIT. See upstream ``server/src/config.rs``,
``server/src/cli.rs`` and ``server/src/engine/mcp_client.rs``. Root ``mcp_servers``
is an array, not a host application's ``mcpServers`` map. ``transport: http``
uses Streamable HTTP, and the supplied URL is the complete endpoint: no ``/mcp``
suffix is added. SSE's current implementation is not a legacy-SSE client.

HTTP endpoints and explicitly supplied bearer-file references are representable.
Bearer-file references require the separately authorized mcp-js change; the
pinned source below does not yet support them. A workload must explicitly select
a compatible tested image before enabling this option.
Rendering cannot determine whether an endpoint requires authentication or works.
In particular, stock weechat-mcp requires HTTP bearer authentication and is not
made compatible by this renderer. Stdio, OAuth and arbitrary headers are deliberately unsupported.
Only file paths, never credential values, are emitted for bearer authentication.

Settings are explicit. Only inline mcp_tools policy chains are accepted as this
renderer API's input; upstream does NOT support inline policies. They are written
as separate .rego artifacts and referenced with supported file:// policy URLs.
See upstream ``server/src/engine/opa.rs`` (PolicySource). The policy inputs
must be supplied even for an empty downstream list. No policy text is invented
or granted automatically. The upstream call path skips its gate when the hook
chain is absent, so an empty policy configuration is unsafe. Policy syntax and
semantics still require validation by the actual runtime. Never embed secrets in
policy text, identifiers, or URL paths: arbitrary opaque secrets are undetectable.

Launch with the returned args, not just --config: CLI values override env vars,
which override file values. In particular the official image supplies
MCP_V8_ALLOWED_HOSTS=*. The image entrypoint is ``mcp-v8``. No live config-reload
API is present; a workload adapter must roll its process when config changes.
Neither rendering nor a matching ConfigMap establishes runtime readiness.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import PurePosixPath
import re
from urllib.parse import unquote, urlsplit

from .providers import DesiredPlan

MCP_JS_SOURCE_COMMIT = "8d1d4f9d568e395b8e3f8dfe5cb17b8ef58ff43b"
MCP_JS_SOURCE_URL = f"https://github.com/r33drichards/mcp-js/tree/{MCP_JS_SOURCE_COMMIT}"
WEECHAT_MCP_SOURCE_COMMIT = "7d934730d6b6864a937035201f6caa039b080792"
DEFAULT_CONFIG_PATH = "/etc/mcp-v8/server.json"
DENY_ALL_MCP_TOOLS = "package mcp.tools\n\ndefault allow := false\n"


class McpConfigError(ValueError):
    """Unsupported or unsafe renderer input; messages never include input values."""


@dataclass(frozen=True, slots=True)
class RenderedMcpConfig:
    """Immutable artifact and argv for an existing mcp-v8 entrypoint."""

    config_json: str
    args: tuple[str, ...]
    policy_files: tuple[tuple[str, str], ...]

    @property
    def digest(self) -> str:
        """Hash the artifact AND invocation, suitable as a rollout input."""
        return hashlib.sha256(
            _json([self.config_json, self.args, self.policy_files]).encode()
        ).hexdigest()


def deny_all_policies() -> dict:
    """An explicit fail-closed choice; never applied as an implicit default."""
    return {"mcp_tools": {"mode": "all", "policies": [{"inline": DENY_ALL_MCP_TOOLS}]}}


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _plain(value) -> bool:
    return (type(value) is str and bool(value)
            and all(not c.isspace() and 32 <= ord(c) != 127 for c in value))


def _port(value) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise McpConfigError("Invalid HTTP port.")
    return value


def _hostname(value: str) -> str:
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        # ASCII DNS labels only, including Kubernetes service DNS names.
        if (len(value) > 253 or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*", value)):
            raise McpConfigError("Invalid endpoint or allowed hostname.") from None
        return value.lower()


def _authority(value: str) -> str:
    if not _plain(value) or any(c in value for c in "*/?#@%\\,"):
        raise McpConfigError("Allowed hosts must be explicit hostnames or authorities.")
    try:
        parsed = urlsplit("//" + value)
        host, port = parsed.hostname, parsed.port
        if not host or parsed.path or value.endswith(":"):
            raise ValueError
        host = _hostname(host)
        if ":" in host:
            # Bracketed IPv6 avoids ambiguity with an optional port.
            if not value.startswith("["):
                raise ValueError
            host = f"[{host}]"
        return host if port is None else f"{host}:{_port(port)}"
    except (ValueError, TypeError):
        raise McpConfigError("Invalid allowed host authority.") from None


def _endpoint(value) -> str:
    if (not _plain(value) or any(c in value for c in "\\?#")
            or not _plain(unquote(value))):
        raise McpConfigError("Invalid HTTP endpoint; queries and fragments are unsupported.")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "@" in unquote(parsed.netloc)
                or "%" in parsed.netloc or parsed.netloc.endswith(":")):
            raise ValueError
        _hostname(parsed.hostname)
        if parsed.port is not None:
            _port(parsed.port)
    except (ValueError, TypeError):
        raise McpConfigError("Invalid HTTP endpoint or embedded credentials.") from None
    # Preserve path/authority exactly; endpoint selection is the caller's decision.
    return value


def _policies(value, directory: str) -> tuple[dict, tuple[tuple[str, str], ...]]:
    # A deliberately small verified subset. No hooks, remote policy fetching,
    # inline credentials, or implicit policy defaults are introduced here.
    if type(value) is not dict or set(value) != {"mcp_tools"}:
        raise McpConfigError("An explicit mcp_tools policy chain is required.")
    operation = value["mcp_tools"]
    if (type(operation) is not dict or set(operation) != {"mode", "policies"}
            or type(operation["mode"]) is not str or operation["mode"] not in {"all", "any"}):
        raise McpConfigError("Unsupported mcp_tools policy configuration.")
    sources = operation["policies"]
    if type(sources) is not list or not sources:
        raise McpConfigError("At least one explicit inline MCP tool policy is required.")
    copied, files = [], []
    for index, source in enumerate(sources):
        if type(source) is not dict or set(source) != {"inline"}:
            raise McpConfigError("Only inline MCP tool policies are supported.")
        policy = source["inline"]
        if (type(policy) is not str or not policy.strip()
                or any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in policy)):
            raise McpConfigError("Invalid inline MCP tool policy.")
        path = str(PurePosixPath(directory) / f"mcp-tools-{index}.rego")
        files.append((path, policy))
        copied.append({"url": "file://" + path, "rule": "data.mcp.tools.allow"})
    return {"mcp_tools": {"mode": operation["mode"], "policies": copied}}, tuple(files)


def render_mcp_config(
    plan: DesiredPlan,
    *,
    bind_host: str,
    http_port: int,
    allowed_hosts: list[str] | tuple[str, ...],
    policies: dict,
    config_path: str = DEFAULT_CONFIG_PATH,
    bearer_token_files: dict[str, str] | None = None,
) -> RenderedMcpConfig:
    """Render a validated MCPClient plan plus explicit runtime settings.

    ``plan.intent['downstreams']`` must be a list of exactly ``{name, url}``
    dictionaries, resolved from Connector IDs and complete MCP endpoint URLs.
    Input is not mutated. Names are sorted and host entries are canonicalized and
    deduplicated; connector URLs and policy-chain order are preserved.

    ``allowed_hosts`` follows upstream Host-header semantics: a bare hostname
    permits every port for that hostname; an authority permits its one port.
    Wildcards and empty allowlists are rejected. IPv6 entries require brackets.
    ``bearer_token_files`` optionally maps configured downstream names to existing
    mounted absolute token-file paths; it requires the compatible mcp-js feature.
    The renderer never reads those files.
    ``endpoint`` is an advertised address, not a bind address or ingress request.
    It is validated but never used to silently configure networking or TLS.
    Mount every ``policy_files`` (absolute path, text) pair alongside the config.
    Policies in this renderer's input use ``inline``; emitted runtime config uses
    ``file://`` URLs, which are the actual upstream PolicySource contract.
    """
    if (not isinstance(plan, DesiredPlan) or plan.identity.kind != "MCPClient"
            or type(plan.intent) is not dict or plan.intent.get("kind") != "MCPClient"):
        raise McpConfigError("Expected an MCPClient desired plan.")
    _endpoint(plan.intent.get("endpoint"))
    try:
        if not _plain(bind_host):
            raise ValueError
        bind = ipaddress.ip_address(bind_host).compressed
    except ValueError:
        raise McpConfigError("Bind host must be an explicit IP address.") from None
    port = _port(http_port)
    if type(allowed_hosts) not in (list, tuple) or not allowed_hosts:
        raise McpConfigError("A nonempty explicit host allowlist is required.")
    hosts = sorted({_authority(host) for host in allowed_hosts})
    if (not _plain(config_path) or not config_path.startswith("/")
            or any(c in config_path for c in "%?#\\")
            or any(part in {".", ".."} for part in config_path.split("/"))
            or not config_path.endswith(".json")):
        raise McpConfigError("Config path must be an absolute JSON file path.")
    policy_config, policy_files = _policies(policies, str(PurePosixPath(config_path).parent))
    downstreams = plan.intent.get("downstreams")
    if type(downstreams) is not list:
        raise McpConfigError("Resolved downstreams are required.")
    servers, names = [], set()
    for downstream in downstreams:
        if type(downstream) is not dict or set(downstream) != {"name", "url"}:
            raise McpConfigError("Only resolved HTTP downstream name and URL are supported.")
        name = downstream["name"]
        if (type(name) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
                or "__" in name or name in names):
            raise McpConfigError("Invalid, duplicate, or ambiguous downstream name.")
        names.add(name)
        servers.append({"name": name, "transport": "http", "url": _endpoint(downstream["url"])})
    if bearer_token_files is not None:
        if type(bearer_token_files) is not dict or set(bearer_token_files) - names:
            raise McpConfigError("Bearer references must identify configured downstreams.")
        for server in servers:
            if server["name"] not in bearer_token_files:
                continue
            token_path = bearer_token_files[server["name"]]
            if (not _plain(token_path) or not token_path.startswith("/")
                    or any(part in {".", "..", ""} for part in token_path.split("/")[1:])
                    or "\\" in token_path):
                raise McpConfigError("Bearer token reference must be an absolute file path.")
            server["auth"] = {"type": "bearer", "token_file": token_path}
    servers.sort(key=lambda server: server["name"])
    config = {
        "http_port": port, "bind_host": bind, "allowed_hosts": hosts,
        "heap_store": "none", "fs_store": "none",
        "mcp_servers": servers, "policies": policy_config,
    }
    # Make every rendered setting win over inherited MCP_V8_* variables. Other
    # runtime capabilities remain the workload adapter's responsibility.
    args = (
        "--config", config_path, "--http-port", str(port), "--bind-host", bind,
        "--allowed-hosts", ",".join(hosts), "--heap-store", "none", "--fs-store", "none",
        "--mcp-config", _json(servers), "--policies-json", _json(policy_config),
    )
    return RenderedMcpConfig(_json(config) + "\n", args, policy_files)
