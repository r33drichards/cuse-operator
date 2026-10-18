"""Offline desired/observed reconciliation; in-memory state is never production readiness."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from cuse_operator.controllers import CONTROLLERS
from cuse_operator.controllers.base import identity
from cuse_operator.providers import (
    InMemoryProvider, Observation, RetryableProviderError, TerminalProviderError, UncertainOutcome,
)
from test_resources import graph as resource_graph, resource  # noqa: F401 -- reusable fixture


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def all_graph(resource_graph):  # noqa: F811 -- pytest fixture injection
    graph = resource_graph
    aggregate = json.loads((Path(__file__).resolve().parents[1] / "examples/architecture.json").read_text())
    graph.append(resource("Architecture", "legacy", aggregate))
    for body in graph:
        body["metadata"]["generation"] = 1
    return graph


def reconcile_graph(graph, provider):
    return {body["kind"]: run(CONTROLLERS[body["kind"]].reconcile(body, graph, provider)) for body in graph}


@pytest.mark.parametrize("kind", [kind for kind in CONTROLLERS if kind != "Architecture"])
def test_each_kind_create_observe_noop_update_and_retain(all_graph, kind):
    provider = InMemoryProvider()
    original = deepcopy(all_graph)
    first = reconcile_graph(all_graph, provider)[kind]
    assert first["ready"] and first["lastAction"] == "Create"
    assert first["desiredRevision"] == first["observedRevision"]
    second = reconcile_graph(all_graph, provider)[kind]
    assert second["ready"] and second["lastAction"] == "NoOp"
    assert all_graph == original
    body = next(b for b in all_graph if b["kind"] == kind)
    if kind == "Architecture":
        body["spec"]["irc"]["endpoint"] = "ircs://changed.example:6697"
    else:
        body["spec"]["id"] += "-updated"
    body["metadata"]["generation"] = 2
    updated = reconcile_graph(all_graph, provider)[kind]
    assert updated["ready"] and updated["lastAction"] == "Update"
    assert updated["desiredRevision"] != first["desiredRevision"]
    assert updated["observedGeneration"] == 2
    before = deepcopy(provider.state)
    deleted = run(CONTROLLERS[kind].on_delete(body, provider))
    assert deleted["reconciliationReason"] == "Retained"
    assert not deleted["ready"] and provider.state == before


def test_transitive_changes_invalidate_every_dependent(all_graph):
    provider = InMemoryProvider()
    before = reconcile_graph(all_graph, provider)
    all_graph[0]["spec"]["endpoint"] = "ircs://new.example:6697"
    # Session sees stale harness / MCP-client revisions and cannot claim readiness.
    blocked = run(CONTROLLERS["Session"].reconcile(all_graph[5], all_graph, provider))
    assert blocked["reconciliationReason"] == "DependencyNotReady"
    assert not blocked["ready"]
    after = reconcile_graph(all_graph, provider)
    for kind in CONTROLLERS:
        if kind == "Architecture":
            assert before[kind]["desiredRevision"] == after[kind]["desiredRevision"]
        else:
            assert before[kind]["desiredRevision"] != after[kind]["desiredRevision"]
            assert after[kind]["ready"] and after[kind]["lastAction"] == "Update"


@pytest.mark.parametrize("kind", list(CONTROLLERS))
def test_disabled_provider_never_implies_readiness(all_graph, kind):
    body = next(b for b in all_graph if b["kind"] == kind)
    result = run(CONTROLLERS[kind].reconcile(body, all_graph))
    assert result["phase"] == "Valid" and not result["ready"]
    assert result["reconciliationReason"] == ("CompatibilityOnly" if kind == "Architecture" else "ProviderNotConfigured")


def test_pending_observed_until_ready_and_children_blocked(all_graph):
    provider = InMemoryProvider(auto_ready=False)
    server, chat = all_graph[:2]
    first = run(CONTROLLERS["IRCServer"].reconcile(server, all_graph, provider))
    assert first["reconciliationReason"] == "Pending"
    assert run(CONTROLLERS["WeeChat"].reconcile(chat, all_graph, provider))["reconciliationReason"] == "DependencyNotReady"
    again = run(CONTROLLERS["IRCServer"].reconcile(server, all_graph, provider))
    assert again["lastAction"] == "NoOp" and provider.ensure_count == 1
    provider.set_ready(identity(server))
    assert run(CONTROLLERS["IRCServer"].reconcile(server, all_graph, provider))["ready"]
    assert run(CONTROLLERS["WeeChat"].reconcile(chat, all_graph, provider))["lastAction"] == "Create"


def test_drift_reapplies_and_uid_recreation_does_not_adopt(all_graph):
    provider = InMemoryProvider()
    body = all_graph[0]
    controller = CONTROLLERS["IRCServer"]
    run(controller.reconcile(body, all_graph, provider))
    old_identity = identity(body)
    provider.drift(old_identity)
    assert run(controller.reconcile(body, all_graph, provider))["lastAction"] == "Update"
    body["metadata"]["uid"] = "new-object-uid"
    assert run(controller.reconcile(body, all_graph, provider))["lastAction"] == "Create"
    assert old_identity in provider.state and identity(body) in provider.state
    assert provider.applied_keys[0] != provider.applied_keys[-1]


@pytest.mark.parametrize("error,reason", [
    (RetryableProviderError, "RetryableProviderError"),
    (TerminalProviderError, "TerminalProviderError"),
    (UncertainOutcome, "UncertainOutcome"), (RuntimeError, "ProviderError"),
])
def test_provider_errors_redact_and_retry_observes_first(all_graph, error, reason):
    class Failing(InMemoryProvider):
        fail = True
        async def ensure(self, desired, *, idempotency_key):
            if self.fail:
                self.fail = False
                raise error("sensitive-value")
            await super().ensure(desired, idempotency_key=idempotency_key)
    provider = Failing()
    body = all_graph[0]
    first = run(CONTROLLERS["IRCServer"].reconcile(body, all_graph, provider))
    assert first["reconciliationReason"] == reason and not first["ready"]
    assert "sensitive-value" not in json.dumps(first)
    observations = provider.observe_count
    assert run(CONTROLLERS["IRCServer"].reconcile(body, all_graph, provider))["ready"]
    assert provider.observe_count > observations


def test_committed_unknown_outcome_is_not_created_twice(all_graph):
    class Unknown(InMemoryProvider):
        async def ensure(self, desired, *, idempotency_key):
            await super().ensure(desired, idempotency_key=idempotency_key)
            raise UncertainOutcome("secret provider response")
    provider = Unknown()
    body = all_graph[0]
    first = run(CONTROLLERS["IRCServer"].reconcile(body, all_graph, provider))
    assert first["reconciliationReason"] == "UncertainOutcome"
    second = run(CONTROLLERS["IRCServer"].reconcile(body, all_graph, provider))
    assert second["lastAction"] == "NoOp" and second["ready"]
    assert provider.ensure_count == 1


def test_invalid_or_missing_dependency_never_calls_provider(all_graph):
    provider = InMemoryProvider()
    body = all_graph[1]
    body["spec"]["nick"] = "\nsensitive-value"
    result = run(CONTROLLERS["WeeChat"].reconcile(body, all_graph, provider))
    assert result["phase"] == "Invalid" and provider.observe_count == 0
    assert "sensitive-value" not in json.dumps(result)
    body["spec"]["nick"] = "valid"
    result = run(CONTROLLERS["WeeChat"].reconcile(body, all_graph[1:], provider))
    assert result["phase"] == "Blocked" and provider.observe_count == 0


def test_untrusted_provider_revision_is_not_echoed(all_graph):
    provider = InMemoryProvider()
    provider.state[identity(all_graph[0])] = Observation(True, "sensitive-value", False)
    result = run(CONTROLLERS["IRCServer"].reconcile(all_graph[0], all_graph, provider))
    assert "sensitive-value" not in json.dumps(result)


def test_transitive_ancestor_readiness_drift_blocks_and_recovers(all_graph):
    provider = InMemoryProvider()
    reconcile_graph(all_graph, provider)
    provider.set_ready(identity(all_graph[0]), False)
    for kind in ("PiHarness", "Session", "Bot"):
        body = next(b for b in all_graph if b["kind"] == kind)
        result = run(CONTROLLERS[kind].reconcile(body, all_graph, provider))
        assert not result["ready"] and result["reconciliationReason"] == "DependencyNotReady"
    provider.set_ready(identity(all_graph[0]))
    assert all(s["ready"] for kind, s in reconcile_graph(all_graph, provider).items() if kind != "Architecture")


def test_provider_timeout_is_retryable_and_cancellation_propagates(all_graph):
    from cuse_operator.controllers.base import Controller
    from cuse_operator.controllers.ircserver import build_plan

    class Hung(InMemoryProvider):
        async def observe(self, identity):
            await asyncio.sleep(100)
    controller = Controller("IRCServer", build_plan, provider_timeout=0.001)
    result = run(controller.reconcile(all_graph[0], all_graph, Hung()))
    assert result["reconciliationReason"] == "RetryableProviderError"
    assert not result["ready"]

    class Cancelled(InMemoryProvider):
        async def observe(self, identity):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(controller.reconcile(all_graph[0], all_graph, Cancelled()))


def test_mcp_plan_resolves_downstream_connector_identity_and_endpoint(all_graph):
    from cuse_operator.controllers.base import PlanContext

    client = all_graph[3]
    plan = CONTROLLERS["MCPClient"].build_plan(client, PlanContext("team", all_graph))
    assert plan.intent["downstreams"] == [{"name": all_graph[2]["spec"]["id"],
                                            "url": all_graph[2]["spec"]["mcp_endpoint"]}]
    assert plan.intent["downstreams"][0]["name"] != all_graph[2]["metadata"]["name"]


def test_architecture_compatibility_never_calls_runtime_provider(all_graph):
    provider = InMemoryProvider()
    result = run(CONTROLLERS["Architecture"].reconcile(all_graph[-1], all_graph, provider))
    assert result["phase"] == "Valid" and not result["ready"]
    assert result["reconciliationReason"] == "CompatibilityOnly"
    assert provider.observe_count == provider.ensure_count == 0
