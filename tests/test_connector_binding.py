import asyncio
from copy import deepcopy

import pytest

from cuse_operator.connector_provider import WeeChatConnectorProvider
from cuse_operator.controllers import CONTROLLERS
from cuse_operator.controllers.base import PlanContext, identity
from cuse_operator.provider_router import ProviderRouter
from cuse_operator.providers import InMemoryProvider, ProviderNotConfigured
from cuse_operator.resources import validate_resource
from test_resources import graph  # noqa: F401


def run(value):
    return asyncio.run(value)


@pytest.fixture
def linked(graph):  # noqa: F811
    graph[2]["spec"].update(weechatRef={"name": "chat", "uid": "chat-uid"},
                            nick=graph[1]["spec"]["nick"], mcp_endpoint=graph[1]["spec"]["mcp_endpoint"])
    return graph


def check(body, inventory):
    return validate_resource(body["kind"], body["spec"], "team", inventory)


def test_optional_link_preserves_canonical_shape(linked):
    assert check(linked[2], linked)["phase"] == "Valid"
    linked[2]["spec"].pop("weechatRef")
    assert check(linked[2], linked)["phase"] == "Valid"


@pytest.mark.parametrize("field,value", [("nick", "different"), ("mcp_endpoint", "https://different.example/mcp")])
def test_link_requires_identical_runtime_identity(linked, field, value):
    linked[2]["spec"][field] = value
    assert check(linked[2], linked)["reason"] == "ReferenceMismatch"


def test_link_missing_deleted_and_recreated_owner(linked):
    assert check(linked[2], [linked[0], linked[2]])["reason"] == "MissingDependency"
    linked[1]["metadata"]["uid"] = "replacement"
    assert check(linked[2], linked)["reason"] == "ReferenceMismatch"
    linked[1]["metadata"]["deletionTimestamp"] = "now"
    assert check(linked[2], linked)["reason"] == "MissingDependency"


def test_link_cannot_cross_namespace(linked):
    linked[2]["spec"]["weechatRef"]["namespace"] = "other"
    assert check(linked[2], linked)["reason"] == "ValidationFailed"


def test_binding_tracks_owner_readiness_without_duplicate_workload(linked):
    runtime = InMemoryProvider()
    binding = WeeChatConnectorProvider(runtime)
    router = ProviderRouter({"IRCServer": runtime, "WeeChat": runtime, "Connector": binding})
    for body in linked[:3]:
        result = run(CONTROLLERS[body["kind"]].reconcile(body, linked, router))
        assert result["ready"]
    assert runtime.ensure_count == 2
    assert all(key.kind != "Connector" for key in runtime.plans)
    runtime.set_ready(identity(linked[1]), False)
    result = run(CONTROLLERS["Connector"].reconcile(linked[2], linked, router))
    assert not result["ready"]
    before = deepcopy(runtime.state)
    run(CONTROLLERS["Connector"].on_delete(linked[2], router))
    assert runtime.state == before


def test_generic_connector_remains_unconfigured(graph):  # noqa: F811
    binding = WeeChatConnectorProvider(InMemoryProvider())
    plan = CONTROLLERS["Connector"].build_plan(graph[2], PlanContext("team", graph))
    binding.bind_plan(plan)
    with pytest.raises(ProviderNotConfigured):
        run(binding.observe(plan.identity))
