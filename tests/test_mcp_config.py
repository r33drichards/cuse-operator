"""Offline config-contract tests; these do not claim a running MCP integration."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

import pytest

from cuse_operator.controllers import CONTROLLERS
from cuse_operator.controllers.base import PlanContext
from cuse_operator.mcp_config import (
    DENY_ALL_MCP_TOOLS, MCP_JS_SOURCE_COMMIT, McpConfigError,
    deny_all_policies, render_mcp_config,
)
from cuse_operator.providers import DesiredPlan, ResourceIdentity
from test_resources import graph  # noqa: F401 -- reusable pytest fixture


def plan(downstreams=None, endpoint="https://client.example/mcp"):
    return DesiredPlan(
        ResourceIdentity("MCPClient", "team", "client", "client-uid"),
        {"kind": "MCPClient", "endpoint": endpoint,
         "downstreams": downstreams if downstreams is not None else [
             {"name": "weechat", "url": "http://connector.team.svc:8000/mcp"},
         ]},
        "a" * 64, "b" * 64,
    )


def render(desired=None, **settings):
    options = {
        "bind_host": "0.0.0.0", "http_port": 8080,
        "allowed_hosts": ["client.team.svc:8080"],
        "policies": deny_all_policies(),
    }
    options.update(settings)
    return render_mcp_config(desired or plan(), **options)


def test_actual_shape_sources_and_launch_arguments():
    output = render()
    config = json.loads(output.config_json)
    assert config == {
        "http_port": 8080, "bind_host": "0.0.0.0",
        "allowed_hosts": ["client.team.svc:8080"],
        "heap_store": "none", "fs_store": "none",
        "mcp_servers": [{"name": "weechat", "transport": "http",
                         "url": "http://connector.team.svc:8000/mcp"}],
        "policies": {"mcp_tools": {"mode": "all", "policies": [
            {"url": "file:///etc/mcp-v8/mcp-tools-0.rego", "rule": "data.mcp.tools.allow"},
        ]}},
    }
    assert len(MCP_JS_SOURCE_COMMIT) == 40
    args = dict(zip(output.args[::2], output.args[1::2], strict=True))
    assert args["--config"] == "/etc/mcp-v8/server.json"
    # These CLI values beat official-image MCP_V8_ALLOWED_HOSTS=* and any
    # inherited env for settings represented by this artifact.
    assert args["--allowed-hosts"] == "client.team.svc:8080"
    assert args["--http-port"] == "8080"
    assert args["--bind-host"] == "0.0.0.0"
    assert args["--heap-store"] == args["--fs-store"] == "none"
    assert json.loads(args["--mcp-config"]) == config["mcp_servers"]
    assert json.loads(args["--policies-json"]) == config["policies"]
    assert "--stateless" not in output.args
    assert len(output.digest) == 64
    assert output.policy_files == (("/etc/mcp-v8/mcp-tools-0.rego", DENY_ALL_MCP_TOOLS),)
    with pytest.raises(FrozenInstanceError):
        output.config_json = "changed"


def test_renders_real_controller_plan_without_mutating_it(graph):  # noqa: F811
    desired = CONTROLLERS["MCPClient"].build_plan(graph[3], PlanContext("team", graph))
    before = deepcopy(desired)
    result = render(desired)
    assert json.loads(result.config_json)["mcp_servers"] == [
        {"name": "connector-domain", "transport": "http",
         "url": "https://connector.example/mcp"},
    ]
    assert desired == before


def test_stable_order_and_host_normalization():
    downstreams = [{"name": "z", "url": "https://z.example/custom/mcp"},
                   {"name": "a", "url": "http://a.team.svc:8000/mcp"}]
    left = render(plan(downstreams), allowed_hosts=["Z.EXAMPLE:8080", "a.example", "a.example"])
    right = render(plan(list(reversed(downstreams))),
                   allowed_hosts=["a.example", "z.example:8080"])
    assert left == right and left.digest == right.digest
    assert [x["name"] for x in json.loads(left.config_json)["mcp_servers"]] == ["a", "z"]


def test_policy_chain_is_explicit_copied_and_ordered():
    policy = {"mcp_tools": {"mode": "all", "policies": [
        {"inline": 'package mcp.tools\nallow if input.tool == "weechat_buffers"\n'},
        {"inline": DENY_ALL_MCP_TOOLS},
    ]}}
    before = deepcopy(policy)
    output = render(policies=policy)
    sources = json.loads(output.config_json)["policies"]["mcp_tools"]["policies"]
    assert sources == [
        {"url": "file:///etc/mcp-v8/mcp-tools-0.rego", "rule": "data.mcp.tools.allow"},
        {"url": "file:///etc/mcp-v8/mcp-tools-1.rego", "rule": "data.mcp.tools.allow"},
    ]
    assert [content for _, content in output.policy_files] == [
        source["inline"] for source in before["mcp_tools"]["policies"]]
    policy["mcp_tools"]["policies"].reverse()
    assert render(policies=policy).digest != output.digest
    # The source references are stable, but changing policy content must roll a
    # workload too, so the digest includes every mounted policy artifact.
    changed = render(policies=policy)
    assert changed.config_json == output.config_json and changed.args == output.args
    assert changed.policy_files != output.policy_files
    assert [content for _, content in output.policy_files] == [
        source["inline"] for source in before["mcp_tools"]["policies"]]
    fresh = deny_all_policies()
    fresh["mcp_tools"]["policies"].clear()
    assert deny_all_policies()["mcp_tools"]["policies"]


def test_empty_downstreams_and_ipv6_and_exact_endpoint_preservation():
    output = render(plan([]), bind_host="::", allowed_hosts=["[::1]:8080", "localhost"])
    config = json.loads(output.config_json)
    assert config["bind_host"] == "::" and config["mcp_servers"] == []
    url = "https://[2001:db8::1]:8443/custom/endpoint%2Fname"
    assert json.loads(render(plan([{"name": "ipv6", "url": url}])).config_json)[
        "mcp_servers"][0]["url"] == url
    root = "http://connector.team.svc:8000/"
    assert json.loads(render(plan([{"name": "root", "url": root}])).config_json)[
        "mcp_servers"][0]["url"] == root


@pytest.mark.parametrize("url", [
    "stdio:python", "sse://connector/mcp", "ftp://connector/mcp", "relative/path", "",
    "http://user:secret@connector/mcp", "http://%75ser%40connector/mcp",
    "http://connector/mcp?token=secret", "http://connector/mcp?ordinary=value",
    "http://connector/mcp#token=secret", "http://connector/mcp#", "http://connector/mcp?",
    "http://connector:0/mcp", "http://connector:65536/mcp", "http://connector:/mcp",
    "http://connector:abc/mcp", "http://connector/\nsecret", "http://connector/%0asecret",
    "http://connector\\other/mcp", "http:///mcp", "http://bad_host/mcp", None, 1,
])
def test_unsupported_or_credential_urls_are_redacted(url):
    with pytest.raises(McpConfigError) as caught:
        render(plan([{"name": "example", "url": url}]))
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("downstreams", [
    None, {}, "not-a-list", [None],
    [{"name": "same", "url": "http://a/mcp"}, {"name": "same", "url": "http://b/mcp"}],
    [{"name": "has__separator", "url": "http://a/mcp"}],
    [{"name": "has space", "url": "http://a/mcp"}],
    [{"name": "has\ncontrol", "url": "http://a/mcp"}],
    [{"name": "", "url": "http://a/mcp"}], [{"name": 7, "url": "http://a/mcp"}],
    [{"name": "a", "url": "http://a/mcp", "auth": {"token": "secret"}}],
    [{"name": "a", "url": "http://a/mcp", "headers": {"Authorization": "secret"}}],
    [{"name": "a", "url": "http://a/mcp", "transport": "http"}],
    [{"name": "a", "command": "python"}],
])
def test_missing_unsupported_or_colliding_bindings(downstreams):
    desired = plan()
    desired.intent["downstreams"] = downstreams
    with pytest.raises(McpConfigError):
        render(desired)


@pytest.mark.parametrize("allowed_hosts", [
    [], "client", ["*"], ["*.example.com"], ["client:*"], ["https://client"],
    ["client/path"], ["client?token=secret"], ["user@client"], ["client:0"],
    ["client:99999"], ["client:"], ["a,b"], [""], [" client"], ["::1"], [None],
])
def test_host_allowlist_fails_closed(allowed_hosts):
    with pytest.raises(McpConfigError):
        render(allowed_hosts=allowed_hosts)


@pytest.mark.parametrize("policies", [
    None, {}, {"mcp_tools": {}}, {"fetch": {}},
    {"mcp_tools": {"mode": "all", "policies": []}},
    {"mcp_tools": {"mode": [], "policies": [{"inline": "x"}]}},
    {"mcp_tools": {"mode": "permissive", "policies": [{"inline": "x"}]}},
    {"mcp_tools": {"mode": "all", "policies": [{"url": "https://policy.example"}]}},
    {"mcp_tools": {"mode": "all", "policies": [{"inline": ""}]}},
    {"mcp_tools": {"mode": "all", "policies": [{"inline": "x\0secret"}]}},
    {"mcp_tools": {"mode": "all", "policies": [{"inline": "x", "headers": {}}]}},
    {"mcp_tools": {"mode": "all", "policies": [{"inline": "x"}], "pre": []}},
])
def test_policies_are_required_without_implicit_grants(policies):
    with pytest.raises(McpConfigError):
        render(policies=policies)


@pytest.mark.parametrize("setting", [
    {"bind_host": "hostname"}, {"bind_host": "*"}, {"bind_host": "0.0.0.0\n"},
    {"http_port": True}, {"http_port": 0}, {"http_port": 65536}, {"http_port": "8080"},
    {"config_path": "relative.json"}, {"config_path": "/etc/../secret.json"},
    {"config_path": "/etc/server.toml"},
])
def test_invalid_runtime_settings(setting):
    with pytest.raises(McpConfigError):
        render(**setting)


def test_wrong_plan_kind_and_bad_advertised_endpoint():
    with pytest.raises(McpConfigError):
        render(plan(endpoint="https://secret@client.example/mcp"))
    desired = plan()
    desired.intent["kind"] = "Connector"
    with pytest.raises(McpConfigError):
        render(desired)


def test_config_mount_path_is_part_of_rollout_digest():
    first = render(config_path="/etc/first.json")
    second = render(config_path="/etc/second.json")
    assert first.config_json == second.config_json
    assert first.args != second.args and first.digest != second.digest


def test_policy_mounts_follow_config_directory():
    output = render(config_path="/custom/config/server.json")
    assert output.policy_files[0][0] == "/custom/config/mcp-tools-0.rego"
    assert json.loads(output.config_json)["policies"]["mcp_tools"]["policies"][0][
        "url"] == "file:///custom/config/mcp-tools-0.rego"


def test_explicit_settings_cannot_be_omitted():
    with pytest.raises(TypeError):
        render_mcp_config(plan())


def test_optional_bearer_file_reference_does_not_read_or_embed_credential():
    desired = plan()
    name = desired.intent["downstreams"][0]["name"]
    output = render(desired, bearer_token_files={name: "/run/secrets/mcp/token"})
    servers = json.loads(output.config_json)["mcp_servers"]
    selected = next(server for server in servers if server["name"] == name)
    assert selected["auth"] == {"type": "bearer", "token_file": "/run/secrets/mcp/token"}
    assert output.digest != render(desired).digest


@pytest.mark.parametrize("path", ["relative", "/run/../token", "/run//token", "/run/token\n", "/run/", "/run\\token"])
def test_bearer_reference_paths_strict(path):
    desired = plan()
    name = desired.intent["downstreams"][0]["name"]
    with pytest.raises(McpConfigError):
        render(desired, bearer_token_files={name: path})


def test_unknown_bearer_binding_rejected():
    with pytest.raises(McpConfigError):
        render(bearer_token_files={"unknown": "/run/token"})
