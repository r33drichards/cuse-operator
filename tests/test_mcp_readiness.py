"""Offline-only protocol/security fixtures: no sockets, credentials or live MCP."""
import asyncio
from dataclasses import replace
import json
import os

import pytest

from cuse_operator.mcp_readiness import (
    AiohttpMCPTransport, MAX_TOKEN_BYTES, MCPHTTPRequest, MCPHTTPResponse,
    MCPReadinessAdapter, MCPReadinessConfigurationError, MCPReadinessSettings,
    PROTOCOL_VERSION, ReadinessResult,
)


ENDPOINT = "https://mcp.example.test/mcp"
INTERNAL = "http://mcp.demo.svc.cluster.local:8080/mcp"
TOKEN = "fixture-only-token-abcdefghijklmnopqrstuvwxyz"


def run(awaitable):
    return asyncio.run(awaitable)


def response(ident, result=None, *, status=200, session=None, sse=False):
    value = {"jsonrpc": "2.0", "id": ident, "result": result}
    headers = {"Content-Type": "text/event-stream" if sse else "application/json"}
    if session is not None:
        headers["Mcp-Session-Id"] = session
    encoded = json.dumps(value).encode()
    if sse:
        encoded = b": heartbeat\r\nevent: message\r\ndata: " + encoded + b"\r\n\r\n"
    return MCPHTTPResponse(status, headers, encoded)


def initialize(**kwargs):
    return response(1, {"protocolVersion": PROTOCOL_VERSION,
                        "serverInfo": {"name": "fixture-mcp", "version": "1"},
                        "capabilities": {"tools": {}}}, **kwargs)


def tool_list(provider="MCPClient", **kwargs):
    names = (["run_js"] if provider == "MCPClient"
             else ["weechat_buffers", "weechat_command_catalog"])
    return response(2, {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                                  for name in names]}, **kwargs)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, request):
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def setup(tmp_path, *, provider="MCPClient", session=None, sse=False):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    config = MCPReadinessSettings(provider, "bearer", token_file=str(token_file))
    responses = [initialize(session=session, sse=sse), MCPHTTPResponse(202, {}, b""),
                 tool_list(provider, sse=sse)]
    if session is not None:
        responses.append(MCPHTTPResponse(200, {}, b""))
    transport = FakeTransport(responses)
    adapter = MCPReadinessAdapter(settings={ENDPOINT: config}, transport=transport)
    return adapter, transport, config, token_file


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("session", [None, "probe-session-id"])
def test_authenticated_initialize_and_tools_list(tmp_path, sse, session):
    adapter, transport, _, _ = setup(tmp_path, session=session, sse=sse)
    result = run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient"))
    assert result == ReadinessResult(True, "mcp-protocol-ready", True, True)
    assert len(transport.calls) == (4 if session else 3)
    posts = [json.loads(call.body) for call in transport.calls if call.method == "POST"]
    assert [post["method"] for post in posts] == [
        "initialize", "notifications/initialized", "tools/list"]
    assert posts[0]["params"]["capabilities"] == {}
    for call in transport.calls:
        assert call.endpoint == ENDPOINT
        assert call.headers["Authorization"] == "Bearer " + TOKEN
        assert call.headers["Accept"] == "application/json, text/event-stream"
        assert call.headers["Accept-Encoding"] == "identity"
        assert TOKEN not in repr(call)
        assert ENDPOINT not in repr(call)
    assert "Mcp-Session-Id" not in transport.calls[0].headers
    for call in transport.calls[1:]:
        assert call.headers.get("Mcp-Session-Id") == session
        assert call.headers["MCP-Protocol-Version"] == PROTOCOL_VERSION
    if session:
        # This only closes the probe's MCP transport session. No resource,
        # workload, bot session or runtime execution is deleted or created.
        assert transport.calls[-1].method == "DELETE"
        assert transport.calls[-1].body is None
        assert transport.calls[-1].headers["Mcp-Session-Id"] == session


def test_weechat_never_claims_irc_readiness_from_buffer_metadata(tmp_path):
    adapter, transport, _, _ = setup(tmp_path, provider="WeeChat", session="probe")
    result = run(adapter.observe(endpoint=ENDPOINT, provider="WeeChat",
                                 expected_server="irc://irc.demo.svc.cluster.local:6667",
                                 expected_nick="example-bot"))
    assert result == ReadinessResult(False, "irc-readiness-unsupported", True, True)
    assert not any(b"tools/call" in (r.body or b"") for r in transport.calls)
    assert not any(b"example-bot" in (r.body or b"") for r in transport.calls)


def test_explicit_internal_unauthenticated_mcpclient():
    config = MCPReadinessSettings("MCPClient", "internal-unauthenticated",
                                 allow_cluster_plaintext=True, cluster_domain="cluster.local")
    transport = FakeTransport([initialize(), MCPHTTPResponse(202, {}, b""), tool_list()])
    adapter = MCPReadinessAdapter(settings={INTERNAL: config}, transport=transport)
    assert run(adapter.observe(endpoint=INTERNAL, provider="MCPClient")) == ReadinessResult(
        True, "mcp-protocol-ready", True, False)
    assert all("Authorization" not in r.headers for r in transport.calls)


def test_explicit_internal_bearer(tmp_path):
    _, transport, config, _ = setup(tmp_path, provider="WeeChat")
    config = replace(config, allow_cluster_plaintext=True, cluster_domain="cluster.local")
    adapter = MCPReadinessAdapter(settings={INTERNAL: config}, transport=transport)
    result = run(adapter.observe(endpoint=INTERNAL, provider="WeeChat"))
    assert result.authenticated is True
    assert result.ready is False


@pytest.mark.parametrize("endpoint", [
    "http://mcp.example.test/mcp", "http://169.254.169.254/latest/meta-data",
    "https://mcp.example.test/mcp/", "https://other.example.test/mcp",
    "https://mcp.example.test:443/mcp", "https://mcp.example.test/mcp?token=x",
    "https://user:pass@mcp.example.test/mcp", None, {},
])
def test_unapproved_cr_urls_do_not_read_files_or_make_requests(tmp_path, monkeypatch, endpoint):
    adapter, transport, _, _ = setup(tmp_path)
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("Unexpected file read"))
    result = run(adapter.observe(endpoint=endpoint, provider="MCPClient"))
    assert result == ReadinessResult(False, "endpoint-not-approved")
    assert transport.calls == []


def test_provider_mismatch_and_absent_transport_do_no_io(tmp_path, monkeypatch):
    adapter, transport, config, _ = setup(tmp_path)
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("Unexpected file read"))
    assert run(adapter.observe(endpoint=ENDPOINT, provider="WeeChat")).ready is False
    absent = MCPReadinessAdapter(settings={ENDPOINT: config}, transport=None)
    assert run(absent.observe(endpoint=ENDPOINT, provider="MCPClient")).reason == (
        "transport-not-configured")
    assert transport.calls == []


def test_constructor_does_not_read_tokens_and_copies_allowlist(monkeypatch):
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("Unexpected file read"))
    settings = {ENDPOINT: MCPReadinessSettings("MCPClient", "bearer", "/missing/token")}
    adapter = MCPReadinessAdapter(settings=settings, transport=None)
    settings["https://other.example.test/mcp"] = settings[ENDPOINT]
    assert run(adapter.observe(endpoint="https://other.example.test/mcp",
                               provider="MCPClient")).reason == "endpoint-not-approved"


@pytest.mark.parametrize("endpoint", [
    "https://user:pass@mcp.example.test/mcp", "https://mcp.example.test/mcp?x=1",
    "https://mcp.example.test/mcp#part", "https://mcp.example.test/%6dcp",
    "https://mcp.example.test/../mcp", "https://mcp.example.test//mcp",
    "https://mcp.example.test/mcp\r\nX:injected", "https://mcp.example.test:/mcp",
    "ftp://mcp.example.test/mcp", "http://mcp.example.test/mcp",
    "http://169.254.169.254/mcp", "https://mcp.example.test:0/mcp",
    "https://mcp.example.test:65536/mcp", "https://mcp.example.test",
])
def test_unsafe_allowlist_entries_rejected(endpoint):
    config = MCPReadinessSettings("MCPClient", "bearer", "/existing/token")
    with pytest.raises(MCPReadinessConfigurationError):
        MCPReadinessAdapter(settings={endpoint: config}, transport=None)


@pytest.mark.parametrize("changes", [
    {"token_file": None}, {"token_file": "relative/token"}, {"token_file": "/x/../token"},
    {"token_file": "/secret/token\n"}, {"provider": "Bot"}, {"auth_mode": "auto"},
    {"timeout_seconds": 0}, {"timeout_seconds": float("inf")}, {"timeout_seconds": 11},
    {"timeout_seconds": True}, {"max_response_bytes": 0}, {"max_response_bytes": 2**30},
    {"allow_cluster_plaintext": "true"}, {"cluster_domain": "*.local"},
])
def test_invalid_settings_rejected(changes):
    config = replace(MCPReadinessSettings("MCPClient", "bearer", "/existing/token"), **changes)
    with pytest.raises(MCPReadinessConfigurationError):
        MCPReadinessAdapter(settings={ENDPOINT: config}, transport=None)


@pytest.mark.parametrize("endpoint", [
    "http://mcp.demo.svc.evil.local/mcp", "http://mcp.demo.svc.cluster.local.evil.test/mcp",
    "http://mcp.svc.cluster.local/mcp", "http://127.0.0.1/mcp",
    "http://mcp.example.test/mcp", "https://mcp.demo.svc.cluster.local/mcp",
])
def test_unauthenticated_requires_exact_internal_service_shape(endpoint):
    config = MCPReadinessSettings("MCPClient", "internal-unauthenticated",
                                 allow_cluster_plaintext=True, cluster_domain="cluster.local")
    with pytest.raises(MCPReadinessConfigurationError):
        MCPReadinessAdapter(settings={endpoint: config}, transport=None)


def test_weechat_cannot_use_unauthenticated_mode():
    config = MCPReadinessSettings("WeeChat", "internal-unauthenticated",
                                 allow_cluster_plaintext=True, cluster_domain="cluster.local")
    with pytest.raises(MCPReadinessConfigurationError):
        MCPReadinessAdapter(settings={INTERNAL: config}, transport=None)


@pytest.mark.parametrize("raw", [
    b"", b"two\nlines", b"token\n\n", b"token\r", b" padded", b"with space",
    b"token\r\nX-Evil: yes", b"\xff", b"=" * 10, b"a=b", b"x" * (MAX_TOKEN_BYTES + 1),
])
def test_invalid_tokens_fail_closed_without_echo(tmp_path, raw):
    adapter, transport, _, token_file = setup(tmp_path)
    token_file.write_bytes(raw)
    result = run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient"))
    assert result.ready is False
    assert result.reason in {"token-file-invalid", "token-file-unavailable"}
    assert transport.calls == []


def test_missing_directory_and_fifo_tokens_do_not_block(tmp_path):
    adapter, transport, _, token_file = setup(tmp_path)
    token_file.unlink()
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).ready is False
    token_file.mkdir()
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).ready is False
    token_file.rmdir()
    os.mkfifo(token_file)
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).reason == "token-file-invalid"
    assert transport.calls == []


def test_projected_secret_symlink_and_rotation_read_on_each_observe(tmp_path):
    adapter, transport, _, token_file = setup(tmp_path)
    target = tmp_path / "projected-token"
    token_file.rename(target)
    token_file.symlink_to(target)
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).ready is True
    target.write_text("second-fixture-token\r\n")
    transport.responses = [initialize(), MCPHTTPResponse(202, {}, b""), tool_list()]
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).ready is True
    assert transport.calls[0].headers["Authorization"] == "Bearer " + TOKEN
    assert transport.calls[3].headers["Authorization"] == "Bearer second-fixture-token"


@pytest.mark.parametrize("status,reason", [
    (401, "authentication-rejected"), (403, "authentication-rejected"),
    (301, "redirect-rejected"), (302, "redirect-rejected"), (307, "redirect-rejected"),
    (404, "http-response-failed"), (500, "http-response-failed"),
])
def test_http_failures_never_follow_or_echo(tmp_path, status, reason):
    adapter, transport, _, _ = setup(tmp_path)
    transport.responses = [MCPHTTPResponse(status, {"Location": "https://evil.test/" + TOKEN},
                                          TOKEN.encode())]
    result = run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient"))
    assert result == ReadinessResult(False, reason)
    assert len(transport.calls) == 1
    assert TOKEN not in repr(result)


@pytest.mark.parametrize("bad", [
    MCPHTTPResponse(200, {"Content-Type": "application/json"}, b"not json"),
    MCPHTTPResponse(200, {"Content-Type": "application/json"}, b'[{"id": 1}]'),
    MCPHTTPResponse(200, {"Content-Type": "text/html"}, b"<html>200 OK</html>"),
    response(99, {}), response(True, {}), response(1, None),
    response(1, {"protocolVersion": "2026-07-28"}),
    response(1, {"protocolVersion": PROTOCOL_VERSION, "serverInfo": {}, "capabilities": {}}),
    MCPHTTPResponse(200, {"Content-Type": "application/json"},
                    b'{"jsonrpc":"2.0","id":1,"id":2,"result":{}}'),
    MCPHTTPResponse(200, {"Content-Type": "application/json"},
                    b'{"jsonrpc":"2.0","id":1,"result":{"x":NaN}}'),
    MCPHTTPResponse(200, {"Content-Type": "application/json"},
                    b'{"jsonrpc":"2.0","id":1,"method":"sampling/createMessage"}'),
    MCPHTTPResponse(200, {"Content-Type": "application/json", "Content-Encoding": "gzip"},
                    b"compressed-data"),
])
def test_protocol_not_tcp_or_200_is_required(tmp_path, bad):
    adapter, transport, _, _ = setup(tmp_path)
    transport.responses = [bad]
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).ready is False
    assert len(transport.calls) == 1


@pytest.mark.parametrize("session", ["", "has space", "x\r\nHeader:evil", "x" * 257, "é"])
def test_invalid_session_id_never_echoed_or_reused(tmp_path, session):
    adapter, transport, _, _ = setup(tmp_path)
    transport.responses = [initialize(session=session)]
    result = run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient"))
    assert result.reason == "invalid-session-id"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("result", [
    {}, {"tools": []}, {"tools": "run_js"}, {"tools": [{"name": "run_js"}]},
    {"tools": [{"name": "run_js", "inputSchema": {}}], "nextCursor": "more"},
    {"tools": [{"name": "run_js", "inputSchema": {}}] * 2},
    {"tools": [{"name": "run_js", "inputSchema": []}]},
])
def test_tools_must_match_verified_provider_contract(tmp_path, result):
    adapter, transport, _, _ = setup(tmp_path)
    transport.responses[2] = response(2, result)
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).ready is False


def test_response_size_is_bounded_even_for_injected_transports(tmp_path):
    adapter, transport, config, _ = setup(tmp_path)
    transport.responses = [MCPHTTPResponse(200, {}, b"x" * (config.max_response_bytes + 1))]
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).reason == "response-too-large"


def test_transport_timeout_is_enforced_and_exception_content_redacted(tmp_path):
    _, _, config, _ = setup(tmp_path)

    class NeverResponds:
        async def request(self, request):
            await asyncio.Event().wait()

    adapter = MCPReadinessAdapter(settings={ENDPOINT: replace(config, timeout_seconds=0.01)},
                                  transport=NeverResponds())
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).reason == "request-failed"
    transport = FakeTransport([RuntimeError(TOKEN)])
    adapter = MCPReadinessAdapter(settings={ENDPOINT: config}, transport=transport)
    result = run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient"))
    assert TOKEN not in repr(result)


def test_rpc_error_echo_and_probe_session_cleanup(tmp_path):
    adapter, transport, _, _ = setup(tmp_path, session="probe-session")
    transport.responses[2] = MCPHTTPResponse(200, {"Content-Type": "application/json"},
        json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"message": TOKEN}}).encode())
    result = run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient"))
    assert result == ReadinessResult(False, "mcp-rpc-error")
    assert transport.calls[-1].method == "DELETE"
    assert TOKEN not in repr(result)
    assert TOKEN not in repr(transport.responses)


@pytest.mark.parametrize("cleanup", [MCPHTTPResponse(405, {}, b""), RuntimeError(TOKEN)])
def test_cleanup_best_effort_never_retries_or_cancels_runtime_work(tmp_path, cleanup):
    adapter, transport, _, _ = setup(tmp_path, session="only-this-probe")
    transport.responses[-1] = cleanup
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).ready is True
    assert len(transport.calls) == 4
    assert transport.calls[-1].headers["Mcp-Session-Id"] == "only-this-probe"


def test_sse_notifications_ignored_but_server_requests_rejected(tmp_path):
    adapter, transport, _, _ = setup(tmp_path, sse=True)
    original = transport.responses[0]
    notification = b'data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
    transport.responses[0] = replace(original, body=notification + original.body)
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).ready is True
    server_request = b'data: {"jsonrpc":"2.0","id":9,"method":"elicitation/create"}\n\n'
    transport.responses = [replace(original, body=server_request + original.body)]
    assert run(adapter.observe(endpoint=ENDPOINT, provider="MCPClient")).reason == (
        "server-request-unsupported")


def mock_aiohttp(monkeypatch, *, headers, chunks, status=200, after_chunks=False):
    """Replace the entire HTTP session factory; this cannot access a socket."""
    calls = {}

    class Content:
        async def iter_chunked(self, size):
            calls["chunk_size"] = size
            for chunk in chunks:
                yield chunk
            if after_chunks:
                raise AssertionError("Must close SSE immediately after correlated response")

    class Response:
        async def __aenter__(self):
            self.status, self.headers, self.content = status, headers, Content()
            return self

        async def __aexit__(self, *args):
            calls["response_closed"] = True

    class Session:
        def __init__(self, **kwargs):
            calls["session"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls["session_closed"] = True

        def request(self, method, endpoint, **kwargs):
            calls["request"] = method, endpoint, kwargs
            return Response()

    monkeypatch.setattr("cuse_operator.mcp_readiness.aiohttp.ClientSession", Session)
    return calls


def test_real_transport_disables_proxies_redirects_cookies_and_compression(monkeypatch):
    reply = initialize()
    calls = mock_aiohttp(monkeypatch, headers=reply.headers, chunks=[reply.body])
    request = MCPHTTPRequest("POST", ENDPOINT, {"Authorization": "Bearer " + TOKEN},
                             b"{}", 2, 4096, 1)
    result = run(AiohttpMCPTransport().request(request))
    assert result == reply
    session = calls["session"]
    assert session["trust_env"] is False
    assert session["auto_decompress"] is False
    assert session["cookie_jar"].__class__.__name__ == "DummyCookieJar"
    assert session["timeout"].total == 2
    method, endpoint, options = calls["request"]
    assert (method, endpoint) == ("POST", ENDPOINT)
    assert options["allow_redirects"] is False
    assert options["proxy"] is None
    assert options["ssl"] is True
    assert calls["response_closed"] and calls["session_closed"]


def test_real_transport_stops_sse_after_response_not_stream_end(monkeypatch):
    reply = initialize(sse=True)
    midpoint = len(reply.body) // 2
    calls = mock_aiohttp(monkeypatch, headers=reply.headers,
                        chunks=[reply.body[:midpoint], reply.body[midpoint:]], after_chunks=True)
    request = MCPHTTPRequest("POST", ENDPOINT, {}, b"{}", 2, 4096, 1)
    assert run(AiohttpMCPTransport().request(request)).body == reply.body
    assert calls["response_closed"]


@pytest.mark.parametrize("headers,chunks", [
    ({"Content-Type": "application/json"}, [b"x" * 1000, b"y" * 1000]),
    ({"Content-Type": "application/json", "Content-Length": "999999"}, []),
    ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, []),
])
def test_real_transport_limits_before_unbounded_parsing(monkeypatch, headers, chunks):
    calls = mock_aiohttp(monkeypatch, headers=headers, chunks=chunks)
    request = MCPHTTPRequest("POST", ENDPOINT, {}, b"{}", 2, 1024, 1)
    with pytest.raises(Exception):
        run(AiohttpMCPTransport().request(request))
    assert calls["response_closed"] and calls["session_closed"]
