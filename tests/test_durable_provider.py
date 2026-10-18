import asyncio
from copy import deepcopy

import pytest

from cuse_operator.controllers import CONTROLLERS
from cuse_operator.controllers.base import PlanContext
from cuse_operator.durable_provider import DurableRuntimeProvider, DurableRuntimeSettings
from cuse_operator.providers import RetryableProviderError, TerminalProviderError, UncertainOutcome
from test_resources import graph  # noqa: F401


def run(value):
    return asyncio.run(value)


class Transport:
    def __init__(self):
        self.state = {}
        self.calls = []
        self.error = None

    async def request(self, method, url, *, token_file, body=None):
        self.calls.append((method, url, token_file, deepcopy(body)))
        if self.error:
            return self.error, {"secret": "must not escape"}
        current = self.state.get(url)
        if method == "GET":
            return (200, current) if current else (404, None)
        assert body["expected_revision"] == (current["revision"] if current else None)
        self.state[url] = {"exists": True, "revision": body["revision"], "ready": False, "phase": "Pending"}
        return 200, self.state[url]


@pytest.fixture
def setup(graph):  # noqa: F811
    plan = CONTROLLERS["Session"].build_plan(graph[5], PlanContext("team", graph))
    transport = Transport()
    provider = DurableRuntimeProvider(DurableRuntimeSettings("https://runtime.example", "/run/secrets/control"), transport=transport)
    provider.bind_plan(plan)
    return provider, transport, plan


def test_create_observe_noop_update_cas_and_retention(setup, graph):  # noqa: F811
    provider, transport, plan = setup
    assert not run(provider.observe(plan.identity)).exists
    run(provider.ensure(plan, idempotency_key=plan.idempotency_key))
    first = run(provider.observe(plan.identity))
    assert first.exists and first.revision == plan.revision and not first.ready
    run(provider.ensure(plan, idempotency_key=plan.idempotency_key))
    assert [c[0] for c in transport.calls].count("PUT") == 1
    url = next(iter(transport.state))
    transport.state[url]["ready"] = True
    assert run(provider.observe(plan.identity)).ready
    graph[5]["spec"]["context_ref"] = "objects/updated"
    updated = CONTROLLERS["Session"].build_plan(graph[5], PlanContext("team", graph))
    run(provider.ensure(updated, idempotency_key=updated.idempotency_key))
    assert transport.calls[-1][3]["expected_revision"] == plan.revision
    assert transport.calls[-1][3]["identity"]["uid"] == plan.identity.uid
    assert all(c[0] != "DELETE" for c in transport.calls)


@pytest.mark.parametrize("status,error", [(401, TerminalProviderError), (403, TerminalProviderError),
                                           (422, TerminalProviderError), (409, RetryableProviderError),
                                           (503, RetryableProviderError), (302, RetryableProviderError)])
def test_safe_read_errors(setup, status, error):
    provider, transport, plan = setup
    transport.error = status
    with pytest.raises(error) as caught:
        run(provider.observe(plan.identity))
    assert "secret" not in str(caught.value)


def test_unknown_write_outcome_is_reobserved_before_retry(setup):
    provider, transport, plan = setup
    original = transport.request

    async def uncertain(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        return (500, None) if method == "PUT" else result

    transport.request = uncertain
    with pytest.raises(UncertainOutcome):
        run(provider.ensure(plan, idempotency_key=plan.idempotency_key))
    transport.request = original
    run(provider.ensure(plan, idempotency_key=plan.idempotency_key))
    assert [c[0] for c in transport.calls].count("PUT") == 1


@pytest.mark.parametrize("endpoint", ["http://runtime.example", "https://u:p@runtime.example", "https://runtime.example?token=x", "https://runtime.example#fragment", "https://runtime.example:0", "https://runtime.example:bad"])
def test_config_rejects_unsafe_endpoint(endpoint):
    with pytest.raises(TerminalProviderError):
        DurableRuntimeSettings(endpoint, "/run/control").validate()


def test_changed_uid_uses_distinct_remote_identity(setup, graph):  # noqa: F811
    provider, transport, old = setup
    run(provider.ensure(old, idempotency_key=old.idempotency_key))
    graph[5]["metadata"]["uid"] = "new-uid"
    new = CONTROLLERS["Session"].build_plan(graph[5], PlanContext("team", graph))
    run(provider.ensure(new, idempotency_key=new.idempotency_key))
    assert len(transport.state) == 2


def test_invalid_response_cannot_claim_ready(setup):
    provider, transport, plan = setup
    run(provider.ensure(plan, idempotency_key=plan.idempotency_key))
    next(iter(transport.state.values()))["ready"] = "true"
    with pytest.raises(RetryableProviderError):
        run(provider.observe(plan.identity))


def test_real_transport_uses_existing_file_no_redirects_or_proxy(monkeypatch, tmp_path):
    from cuse_operator.durable_provider import AioHttpControlTransport
    import cuse_operator.durable_provider as module
    import json

    token = "test-only-noncredential-" + "a" * 32
    path = tmp_path / "token"
    path.write_text(token + "\n")
    calls = []
    response_bytes = json.dumps({"exists": True, "revision": "a" * 64, "ready": True}).encode()

    class Response:
        status = 200
        content = None

        def __init__(self):
            self.content = self

        async def iter_chunked(self, size):
            yield response_bytes[:3]
            yield response_bytes[3:]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["timeout"].total == 30

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"]["Authorization"] == "Bearer " + token
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    status, value = run(AioHttpControlTransport().request("GET", "https://runtime.example", token_file=str(path)))
    assert status == 200 and value["ready"]
    assert len(calls) == 1


def test_transport_missing_or_invalid_file_is_generic(tmp_path):
    from cuse_operator.durable_provider import AioHttpControlTransport
    from cuse_operator.providers import ProviderNotConfigured

    for value in [None, "short", "a" * 32 + "\nembedded", "a" * 9000]:
        path = tmp_path / "token"
        if value is not None:
            path.write_text(value)
        with pytest.raises(ProviderNotConfigured) as caught:
            run(AioHttpControlTransport().request("GET", "https://runtime.example", token_file=str(path)))
        assert str(path) not in str(caught.value)
        if value:
            assert value not in str(caught.value)


def test_transport_rejects_fifo_without_blocking(tmp_path):
    import os
    from cuse_operator.durable_provider import AioHttpControlTransport
    from cuse_operator.providers import ProviderNotConfigured
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(ProviderNotConfigured):
        run(AioHttpControlTransport().request("GET", "https://runtime.example", token_file=str(path)))
