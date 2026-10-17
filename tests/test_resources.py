"""Pure split-resource contracts, without Kubernetes or network I/O."""
from copy import deepcopy

import pytest

from cuse_operator.resources import (
    PLURALS, RESOURCE_SPECS, ResourceSpecError, validate_resource, validate_resource_spec,
)


def resource(kind, name, spec, namespace="team", uid=None):
    return {"apiVersion": "cuse.io/v1alpha1", "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "uid": uid or name + "-uid"}, "spec": spec}


@pytest.fixture
def graph():
    return [
        resource("IRCServer", "irc", {"id": "server-domain", "endpoint": "irc://irc.example:6697"}),
        resource("WeeChat", "chat", {"id": "chat-domain", "serverRef": {"name": "irc"}, "nick": "dot", "mcp_endpoint": "https://chat.example/mcp"}),
        resource("Connector", "connector", {"id": "connector-domain", "serverRef": {"name": "irc"}, "nick": "helper", "mcp_endpoint": "https://connector.example/mcp"}),
        resource("MCPClient", "client", {"id": "client-domain", "endpoint": "https://client.example", "connectorRefs": [{"name": "connector"}]}),
        resource("PiHarness", "harness", {"id": "harness-domain", "user_id": "owner", "weechatRef": {"name": "chat"}}),
        resource("Session", "session", {"id": "session-domain", "harnessRef": {"name": "harness"}, "channel": {"serverRef": {"name": "irc"}, "name": "#room"}, "context_ref": "objects/context", "share_url": "https://share.example/session", "mcpClientRef": {"name": "client"}}),
        resource("Bot", "bot", {"id": "bot-domain", "weechatRef": {"name": "chat"}, "harnessRef": {"name": "harness"}}),
    ]


def check(body, graph):
    return validate_resource(body["kind"], body["spec"], body["metadata"]["namespace"], graph)


def test_all_seven_contracts_are_valid_and_immutable(graph):
    before = deepcopy(graph)
    assert set(RESOURCE_SPECS) == set(PLURALS) == {r["kind"] for r in graph}
    assert "Architecture" not in RESOURCE_SPECS
    for body in graph:
        assert check(body, graph)["reason"] == "Validated"
    assert graph == before


def test_mcp_connectors_optional(graph):
    graph[3]["spec"].pop("connectorRefs")
    assert check(graph[5], graph)["phase"] == "Valid"
    assert RESOURCE_SPECS["MCPClient"]["properties"]["connectorRefs"]["default"] == []


@pytest.mark.parametrize("change", [
    {"namespace": "other"}, {"extra": "unexpected"}, {"name": 123},
    {"name": ""}, {"uid": ""}, {"name": "has spaces"}, {"name": "UPPER"}, {"name": "bad..name"},
])
def test_references_strictly_namespaced_and_typed(graph, change):
    graph[1]["spec"]["serverRef"].update(change)
    assert check(graph[1], graph)["reason"] == "ValidationFailed"


@pytest.mark.parametrize("kind,spec", [
    ("Architecture", {}), ("IRCServer", {"id": True, "endpoint": "https://example.com"}),
    ("IRCServer", {"id": "ok", "endpoint": "relative"}),
    ("IRCServer", {"id": "ok", "endpoint": "https://example.com", "extra": "secret"}),
    ("MCPClient", {"id": "ok", "endpoint": "https://example.com", "connectorRefs": ()}),
    ("PiHarness", {"id": "ok", "user_id": "bad\n", "weechatRef": {"name": "chat"}}),
])
def test_invalid_local_contracts(kind, spec):
    assert validate_resource(kind, spec, "team", [])["reason"] == "ValidationFailed"
    with pytest.raises(ResourceSpecError, match="^Invalid resource specification.$"):
        validate_resource_spec(kind, spec)


@pytest.mark.parametrize("missing", [0, 1, 2, 3, 4])
def test_session_resolves_full_dependency_graph(graph, missing):
    body = graph[5]
    graph.pop(missing)
    assert check(body, graph)["phase"] == "Blocked"
    assert check(body, graph)["reason"] == "MissingDependency"


@pytest.mark.parametrize("deleting", [0, 1, 2, 3, 4])
def test_deleting_transitive_dependencies_block(graph, deleting):
    graph[deleting]["metadata"]["deletionTimestamp"] = "2026-10-17T00:00:00Z"
    assert check(graph[5], graph)["reason"] == "MissingDependency"


def test_reference_does_not_cross_namespace(graph):
    graph[0]["metadata"]["namespace"] = "other"
    assert check(graph[5], graph)["reason"] == "MissingDependency"


def test_uid_pin_matches_object_identity(graph):
    graph[1]["spec"]["serverRef"]["uid"] = "irc-uid"
    assert check(graph[5], graph)["phase"] == "Valid"
    graph[0]["metadata"]["uid"] = "replacement-uid"
    assert check(graph[5], graph)["reason"] == "ReferenceMismatch"


@pytest.mark.parametrize("index", range(7))
def test_all_conflicting_domain_identities_invalid(graph, index):
    original = graph[index]
    duplicate = deepcopy(original)
    duplicate["metadata"]["name"] += "-duplicate"
    graph.append(duplicate)
    assert check(original, graph)["reason"] == "DuplicateIdentity"
    assert check(duplicate, graph)["reason"] == "DuplicateIdentity"
    duplicate["metadata"]["namespace"] = "other"
    assert check(original, graph)["phase"] == "Valid"


def test_identity_scope_is_per_kind(graph):
    for body in graph:
        body["spec"]["id"] = "shared"
    assert all(check(body, graph)["phase"] == "Valid" for body in graph)


def test_duplicate_inventory_entry_is_not_duplicate_identity(graph):
    graph.append(deepcopy(graph[0]))
    assert check(graph[5], graph)["phase"] == "Valid"


def test_duplicate_transitive_dependency_invalidates_root(graph):
    duplicate = deepcopy(graph[0])
    duplicate["metadata"]["name"] = "another-irc"
    graph.append(duplicate)
    assert check(graph[5], graph)["reason"] == "DuplicateIdentity"


def test_bot_harness_relationship_uses_resolved_domain_ids(graph):
    second = deepcopy(graph[1])
    second["metadata"]["name"] = "other-chat"
    second["spec"]["id"] = "other-chat-domain"
    graph.append(second)
    graph[4]["spec"]["weechatRef"] = {"name": "other-chat"}
    assert check(graph[6], graph)["reason"] == "ReferenceMismatch"


@pytest.mark.parametrize("target", ["channel", "connector", "weechat"])
def test_session_server_relationships(graph, target):
    graph.append(resource("IRCServer", "other-irc", {"id": "other-server", "endpoint": "irc://other.example"}))
    if target == "channel":
        graph[5]["spec"]["channel"]["serverRef"] = {"name": "other-irc"}
    else:
        graph[2 if target == "connector" else 1]["spec"]["serverRef"] = {"name": "other-irc"}
    assert check(graph[5], graph)["reason"] == "ReferenceMismatch"


def test_transitive_invalid_spec_not_trusted_status(graph):
    graph[0]["status"] = {"phase": "Valid", "reason": "Validated"}
    graph[0]["spec"]["endpoint"] = "https://user:secret@example.com"
    status = check(graph[5], graph)
    assert status["reason"] == "ValidationFailed"
    assert "secret" not in str(status)
    assert "example.com" not in str(status)


def test_status_messages_never_contain_ref_or_domain_input(graph):
    graph[5]["spec"]["harnessRef"]["name"] = "private-name"
    graph[5]["spec"]["id"] = "private-id"
    status = check(graph[5], graph)
    assert status["reason"] == "MissingDependency"
    assert "private" not in str(status)


def test_invalid_local_fields_take_precedence_over_missing_dependencies(graph):
    graph[5]["spec"]["channel"]["name"] = "not-a-channel"
    assert check(graph[5], [graph[5]])["reason"] == "ValidationFailed"


def test_duplicate_connector_refs_are_invalid(graph):
    graph[3]["spec"]["connectorRefs"] *= 2
    assert check(graph[3], graph)["reason"] == "ValidationFailed"
