"""Bounded, opt-in Streamable HTTP readiness, with an injected transport.

Contracts inspected at mcp-js 8d1d4f9d568e395b8e3f8dfe5cb17b8ef58ff43b
(``server/src/main.rs`` and ``mcp.rs``) and
weechat-mcp 7d934730d6b6864a937035201f6caa039b080792 (``bridge.py``,
``weechat_mcp.py`` and ``native_buffer.py``). Both serve /mcp; no path is
appended here. Protocol: modelcontextprotocol.io/specification/2025-03-26.

An exact endpoint allowlist is operator configuration, never populated from CR
URLs. Bearer references are to EXISTING files mounted in the operator process,
not to paths inside managed Pods. No Kubernetes Secret reads or writes occur.
Constructors do not read tokens or open connections. Inject AiohttpMCPTransport
explicitly for real requests, or a fake for entirely offline tests.

Only initialize, notifications/initialized and tools/list are sent. A session
created by this probe is deleted best-effort. No tools/call, prompts, execution,
legacy SSE discovery, OAuth, redirects, proxy discovery or automatic retries.
MCPClient success establishes its inbound protocol only, not its downstreams:
upstream can advertise tool stubs. WeeChat cannot become ready from this source
revision: safe reads expose buffer local variables and historical lines, not
live IRC connection/registration or the remote endpoint. They can survive a
disconnect. After authenticated protocol validation, return the explicit
irc-readiness-unsupported result instead of inventing connection evidence.

Results and exceptions contain fixed reason codes only. HTTP bodies, session
IDs, bearer contents and exception text never reach status/logging here. An
injected transport is trusted code and must not log request headers or bodies.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
import re
import stat
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import urlsplit

import aiohttp


PROTOCOL_VERSION = "2025-03-26"
MAX_TOKEN_BYTES = 8192
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DNS = rf"{_LABEL}(?:\.{_LABEL})*"
_REQUIRED_TOOLS = {
    "MCPClient": frozenset({"run_js"}),
    "WeeChat": frozenset({"weechat_buffers", "weechat_command_catalog"}),
}


class MCPReadinessConfigurationError(ValueError):
    """Invalid operator configuration; never contains supplied values."""


class _ProbeFailure(Exception):
    """Internal fixed reason code, never an external response or exception."""


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    reason: str
    protocol_ready: bool = False
    authenticated: bool = False


@dataclass(frozen=True, slots=True)
class MCPReadinessSettings:
    """Explicit policy for one exact allowlisted endpoint.

    Plaintext requires both an explicit opt-in and a matching fully qualified
    Kubernetes Service name (<service>.<namespace>.svc.<cluster_domain>).
    This does not provide network isolation; the deployer must arrange that.
    internal-unauthenticated is restricted to an internal MCPClient endpoint.
    ``authenticated`` in the result means a configured bearer was accepted;
    this probe does not audit the server's authorization enforcement.
    """

    provider: str
    auth_mode: str
    token_file: str | None = field(default=None, repr=False)
    allow_cluster_plaintext: bool = False
    cluster_domain: str | None = None
    timeout_seconds: float = 3.0
    max_response_bytes: int = 256 * 1024

    def validate(self, endpoint: str) -> None:
        if (type(self.provider) is not str or self.provider not in _REQUIRED_TOOLS
                or self.auth_mode not in ("bearer", "internal-unauthenticated")
                or type(self.allow_cluster_plaintext) is not bool
                or type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 10
                or type(self.max_response_bytes) is not int
                or not 1024 <= self.max_response_bytes <= 1024 * 1024):
            raise MCPReadinessConfigurationError("Invalid MCP readiness settings.")
        try:
            if (type(endpoint) is not str or not endpoint.isascii()
                    or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in endpoint)
                    or any(c in endpoint for c in "\\?#%@") or len(endpoint) > 2048):
                raise ValueError
            parsed = urlsplit(endpoint)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.netloc.endswith(":")
                    or parsed.port is not None and not 1 <= parsed.port <= 65535
                    or not re.fullmatch(r"/[A-Za-z0-9._~/-]*", parsed.path)
                    or "//" in parsed.path
                    or any(p in (".", "..") for p in parsed.path.split("/"))):
                raise ValueError
            host = parsed.hostname
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if len(host) > 253 or not re.fullmatch(_DNS, host):
                    raise ValueError from None
            if self.cluster_domain is not None and (
                    type(self.cluster_domain) is not str
                    or len(self.cluster_domain) > 253
                    or not re.fullmatch(_DNS, self.cluster_domain)):
                raise ValueError
            internal = (self.cluster_domain is not None and re.fullmatch(
                rf"{_LABEL}\.{_LABEL}\.svc\.{re.escape(self.cluster_domain)}", host))
            if parsed.scheme == "http" and not (self.allow_cluster_plaintext and internal):
                raise ValueError
            if self.auth_mode == "internal-unauthenticated" and not (
                    self.provider == "MCPClient" and internal
                    and self.allow_cluster_plaintext and parsed.scheme == "http"
                    and self.token_file is None):
                raise ValueError
            if self.auth_mode == "bearer" and (
                    type(self.token_file) is not str or not self.token_file.startswith("/")
                    or not self.token_file.isascii() or len(self.token_file) > 4096
                    or any(ord(c) < 33 or ord(c) == 127 for c in self.token_file)
                    or "\\" in self.token_file
                    or any(p in (".", "..") for p in self.token_file.split("/"))):
                raise ValueError
        except (ValueError, TypeError):
            raise MCPReadinessConfigurationError("Unsafe MCP endpoint or auth settings.") from None


@dataclass(frozen=True, slots=True)
class MCPHTTPRequest:
    """Secret-bearing transport input; its repr omits all remote/private data."""

    method: str
    endpoint: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    body: bytes | None = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int
    expected_id: int | None = None


@dataclass(frozen=True, slots=True)
class MCPHTTPResponse:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


class MCPTransport(Protocol):
    async def request(self, request: MCPHTTPRequest) -> MCPHTTPResponse:
        """Return bounded raw bytes; do not follow redirects or discover proxies."""
        ...


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = [value for key, value in headers.items() if key.lower() == name]
    if len(values) > 1 or any(type(value) is not str for value in values):
        raise _ProbeFailure("invalid-response")
    return values[0] if values else None


def _json(data: bytes | str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError

    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _ProbeFailure("invalid-response") from None


def _rpc_result(value, expected_id: int) -> dict | None:
    if type(value) is not dict or value.get("jsonrpc") != "2.0":
        raise _ProbeFailure("invalid-response")
    if "method" in value:
        if type(value["method"]) is not str or "result" in value or "error" in value:
            raise _ProbeFailure("invalid-response")
        if "id" in value:
            # Never satisfy server-originated sampling, elicitation or tool requests.
            raise _ProbeFailure("server-request-unsupported")
        return None
    if type(value.get("id")) is not int or value["id"] != expected_id:
        raise _ProbeFailure("invalid-response")
    if "error" in value:
        raise _ProbeFailure("mcp-rpc-error")
    if type(value.get("result")) is not dict:
        raise _ProbeFailure("invalid-response")
    return value["result"]


def _sse_result(body: bytes, expected_id: int) -> dict | None:
    """Parse complete SSE events only; a missing final blank line is incomplete."""
    # Split bytes before UTF-8 decoding so an incomplete multibyte tail can wait.
    normalized = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for event in normalized.split(b"\n\n")[:-1]:
        data = []
        for line in event.split(b"\n"):
            if line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
            elif line == b"data":
                data.append(b"")
        if not data:
            continue
        result = _rpc_result(_json(b"\n".join(data)), expected_id)
        if result is not None:
            return result
    return None


class AiohttpMCPTransport:
    """Explicit real transport, isolated per request; never automatically used.

    TLS verification remains enabled. Ambient proxies, netrc credentials,
    cookies, redirects and compression are disabled. Both socket time and a
    total deadline are bounded. An SSE response closes immediately after the
    correlated response, without keeping an event subscription open.
    """

    async def request(self, request: MCPHTTPRequest) -> MCPHTTPResponse:
        timeout = aiohttp.ClientTimeout(total=request.timeout_seconds,
                                        connect=min(request.timeout_seconds, 2.0),
                                        sock_read=request.timeout_seconds)
        try:
            async with aiohttp.ClientSession(
                    timeout=timeout, trust_env=False, auto_decompress=False,
                    cookie_jar=aiohttp.DummyCookieJar()) as session:
                async with session.request(
                        request.method, request.endpoint, headers=dict(request.headers),
                        data=request.body, allow_redirects=False, proxy=None, ssl=True,
                        max_line_size=8192, max_field_size=8192) as response:
                    headers = response.headers.copy()
                    # No response body from an error/redirect is needed or retained.
                    if response.status != 200:
                        return MCPHTTPResponse(response.status, headers, b"")
                    encoding = _header(headers, "content-encoding")
                    if encoding not in (None, "identity"):
                        raise _ProbeFailure("unsupported-content-encoding")
                    content_length = _header(headers, "content-length")
                    if content_length is not None and (
                            not content_length.isdecimal()
                            or int(content_length) > request.max_response_bytes):
                        raise _ProbeFailure("response-too-large")
                    body = bytearray()
                    is_sse = (_header(headers, "content-type") or "").split(";", 1)[0].strip()
                    async for chunk in response.content.iter_chunked(8192):
                        if len(body) + len(chunk) > request.max_response_bytes:
                            raise _ProbeFailure("response-too-large")
                        body.extend(chunk)
                        if (is_sse == "text/event-stream" and request.expected_id is not None
                                and _sse_result(bytes(body), request.expected_id) is not None):
                            break
                    return MCPHTTPResponse(response.status, headers, bytes(body))
        except _ProbeFailure:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            raise _ProbeFailure("request-failed") from None


def _authorization(token_file: str) -> str:
    """Read one bounded regular file; projected-secret symlinks are supported."""
    fd = None
    try:
        fd = os.open(token_file, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _ProbeFailure("token-file-invalid")
        with os.fdopen(fd, "rb") as source:
            fd = None
            raw = source.read(MAX_TOKEN_BYTES + 1)
        if len(raw) > MAX_TOKEN_BYTES:
            raise _ProbeFailure("token-file-invalid")
        token = raw.decode("ascii")
        if token.endswith("\r\n"):
            token = token[:-2]
        elif token.endswith("\n"):
            token = token[:-1]
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise _ProbeFailure("token-file-invalid")
        return "Bearer " + token
    except (OSError, UnicodeError, ValueError):
        raise _ProbeFailure("token-file-unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


class MCPReadinessAdapter:
    """Readiness observer with explicit endpoint selection and no live defaults."""

    def __init__(self, *, settings: Mapping[str, MCPReadinessSettings],
                 transport: MCPTransport | None):
        copied = dict(settings)
        for endpoint, config in copied.items():
            if not isinstance(config, MCPReadinessSettings):
                raise MCPReadinessConfigurationError("Invalid MCP readiness settings.")
            config.validate(endpoint)
        self._settings = MappingProxyType(copied)
        self._transport = transport

    async def _request(self, endpoint, config, headers, *, message=None, method="POST"):
        expected_id = message.get("id") if message else None
        body = (json.dumps(message, separators=(",", ":"), allow_nan=False).encode()
                if message is not None else None)
        request = MCPHTTPRequest(method, endpoint, MappingProxyType(dict(headers)), body,
                                 config.timeout_seconds, config.max_response_bytes, expected_id)
        async with asyncio.timeout(config.timeout_seconds):
            response = await self._transport.request(request)
        if (not isinstance(response, MCPHTTPResponse) or type(response.status) is not int
                or not isinstance(response.headers, Mapping) or type(response.body) is not bytes):
            raise _ProbeFailure("invalid-response")
        if len(response.body) > config.max_response_bytes:
            raise _ProbeFailure("response-too-large")
        if response.status in (401, 403):
            raise _ProbeFailure("authentication-rejected")
        if 300 <= response.status < 400:
            raise _ProbeFailure("redirect-rejected")
        if method == "DELETE":
            return response
        if expected_id is None:
            if response.status != 202 or response.body:
                raise _ProbeFailure("invalid-notification-response")
            return response
        if response.status != 200:
            raise _ProbeFailure("http-response-failed")
        if _header(response.headers, "content-encoding") not in (None, "identity"):
            raise _ProbeFailure("unsupported-content-encoding")
        return response

    @staticmethod
    def _result(response, expected_id):
        content_type = (_header(response.headers, "content-type") or "").split(";", 1)[0].strip()
        if content_type == "application/json":
            result = _rpc_result(_json(response.body), expected_id)
        elif content_type == "text/event-stream":
            result = _sse_result(response.body, expected_id)
        else:
            raise _ProbeFailure("unsupported-content-type")
        if result is None:
            raise _ProbeFailure("incomplete-response")
        return result

    async def observe(self, *, endpoint: str, provider: str,
                      expected_server: str | None = None,
                      expected_nick: str | None = None) -> ReadinessResult:
        """Observe only an exact approved endpoint; unknown CR URLs cause no IO.

        expected_server/nick express the workload's desired IRC identity. They
        are intentionally never sent to a server: pinned WeeChat has no safe
        proof for them, so it cannot produce ready=True. No buffer history is
        read merely to provide weaker evidence.
        """
        if type(endpoint) is not str or type(provider) is not str:
            return ReadinessResult(False, "endpoint-not-approved")
        config = self._settings.get(endpoint)
        if config is None or config.provider != provider:
            return ReadinessResult(False, "endpoint-not-approved")
        if self._transport is None:
            return ReadinessResult(False, "transport-not-configured")
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", "Accept-Encoding": "identity"}
        has_session = False
        try:
            if config.auth_mode == "bearer":
                headers["Authorization"] = _authorization(config.token_file)
            initialized = await self._request(endpoint, config, headers, message={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "cuse-readiness", "version": "0.1.0"}}})
            session_id = _header(initialized.headers, "mcp-session-id")
            if session_id is not None:
                if not 1 <= len(session_id) <= 256 or any(
                        not 33 <= ord(c) <= 126 for c in session_id):
                    raise _ProbeFailure("invalid-session-id")
                headers["Mcp-Session-Id"] = session_id
                has_session = True
            result = self._result(initialized, 1)
            if result.get("protocolVersion") != PROTOCOL_VERSION:
                raise _ProbeFailure("protocol-version-unsupported")
            info, capabilities = result.get("serverInfo"), result.get("capabilities")
            if (type(info) is not dict or any(type(info.get(k)) is not str or not info[k]
                                             for k in ("name", "version"))
                    or type(capabilities) is not dict or type(capabilities.get("tools")) is not dict):
                raise _ProbeFailure("invalid-initialize-result")
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
            await self._request(endpoint, config, headers, message={
                "jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = await self._request(endpoint, config, headers, message={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            result = self._result(listed, 2)
            if result.get("nextCursor") is not None:
                raise _ProbeFailure("tools-pagination-unsupported")
            tools = result.get("tools")
            if type(tools) is not list or len(tools) > 1024:
                raise _ProbeFailure("invalid-tools-result")
            names = set()
            for tool in tools:
                if (type(tool) is not dict or type(tool.get("name")) is not str
                        or not 1 <= len(tool["name"]) <= 256 or tool["name"] in names
                        or type(tool.get("inputSchema")) is not dict):
                    raise _ProbeFailure("invalid-tools-result")
                names.add(tool["name"])
            if not _REQUIRED_TOOLS[provider] <= names:
                raise _ProbeFailure("required-tools-missing")
            authenticated = config.auth_mode == "bearer"
            if provider == "WeeChat":
                return ReadinessResult(False, "irc-readiness-unsupported", True, authenticated)
            return ReadinessResult(True, "mcp-protocol-ready", True, authenticated)
        except _ProbeFailure as failure:
            return ReadinessResult(False, failure.args[0])
        except Exception:
            # Includes injected transport failures. Never reflect their text,
            # URL, response body, headers, request object or token into status.
            return ReadinessResult(False, "request-failed")
        finally:
            if has_session:
                try:
                    await self._request(endpoint, config, headers, method="DELETE")
                except Exception:
                    pass  # Only this probe's session; no retries or follow-up actions.
