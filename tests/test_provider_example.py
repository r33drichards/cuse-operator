"""Validate the deliberately nondeployable provider example without runtime I/O."""
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from cuse_operator.controllers import CONTROLLERS
from cuse_operator.controllers.base import PlanContext
from cuse_operator.durable_provider import DurableRuntimeProvider
from cuse_operator.mcp_readiness import MCPReadinessAdapter
from cuse_operator.mcp_workload_provider import SecretKeyRef
from cuse_operator.runtime import build_provider, load_provider_config


EXAMPLE = Path(__file__).parents[1] / "examples" / "provider-config.json"
RESOURCES = EXAMPLE.with_name("resources.json")


def write_config(tmp_path, value):
    path = tmp_path / "provider-config.json"
    path.write_text(json.dumps(value))
    return str(path)


def test_example_load_and_build_have_no_credential_network_or_kubernetes_io(monkeypatch):
    import aiohttp
    import cuse_operator.mcp_readiness as readiness

    def forbidden(*args, **kwargs):
        raise AssertionError("Constructing providers must not perform runtime I/O")

    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(readiness, "_authorization", forbidden)
    original_open, opened = Path.open, []

    def config_only(path, *args, **kwargs):
        assert path == EXAMPLE, "Credential/reference files must not be read"
        opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", config_only)
    monkeypatch.setenv("CUSE_PROVIDER_CONFIG", str(EXAMPLE))
    config = load_provider_config()
    # Opaque clients expose no read/write API, so any unexpected access fails.
    router = build_provider(config, apps=object(), core=object())
    assert opened == [EXAMPLE]
    assert set(router.providers) == {
        "IRCServer", "WeeChat", "MCPClient", "PiHarness", "Session", "Connector", "Bot"}
    assert isinstance(router.providers["PiHarness"], DurableRuntimeProvider)
    assert router.providers["Session"] is router.providers["PiHarness"]
    assert isinstance(router.providers["WeeChat"].readiness, MCPReadinessAdapter)
    assert router.providers["WeeChat"].readiness is router.providers["MCPClient"].readiness


def test_example_has_only_nonexistent_images_and_explicit_secret_references():
    config = load_provider_config(str(EXAMPLE))
    for section in ("ircservers", "weechats", "mcpclients"):
        assert len(config[section]) == 1
        image = config[section][0]["image"]
        assert re.fullmatch(r"registry\.example\.invalid/cuse/[a-z0-9-]+@sha256:[a-f0-9]{64}", image)
    assert config["durable"]["endpoint"] == "https://pi-control.example.invalid"
    weechat, mcp = config["weechats"][0], config["mcpclients"][0]
    assert weechat["auth_secret"] == {"name": "example-weechat-bearer", "key": "token"}
    assert mcp["secret_profiles"] == {"connector-demo": weechat["auth_secret"]}
    assert weechat["channels"] == ["#example"]
    assert weechat["full_control"] is False
    assert weechat["storage_mode"] == mcp["storage_mode"] == "ephemeral"
    assert mcp["policies"]["mcp_tools"]["policies"] == [
        {"inline": "package mcp.tools\n\ndefault allow := false\n"}]
    assert {entry["provider"] for entry in config["readiness"]} == {"WeeChat", "MCPClient"}


def test_json_arrays_and_reference_objects_become_valid_explicit_settings():
    config = load_provider_config(str(EXAMPLE))
    router = build_provider(config, apps=object(), core=object(), readiness=object())
    chat = router.providers["WeeChat"].settings[("default", "weechat")]
    client = router.providers["MCPClient"].settings[("default", "mcp-client")]
    assert list(chat.channels) == ["#example"]
    assert list(chat.allowed_hosts) == ["weechat.default.svc.cluster.local:8080"]
    assert chat.auth_secret == SecretKeyRef("example-weechat-bearer", "token")
    assert client.secret_profiles["connector-demo"] == chat.auth_secret
    assert client.supports_bearer_file is True


def test_structural_resource_names_match_and_explicit_runtime_adjustments_bind():
    config = load_provider_config(str(EXAMPLE))
    router = build_provider(config, apps=object(), core=object(), readiness=object())
    inventory = deepcopy(json.loads(RESOURCES.read_text())["items"])
    index = {resource["kind"]: resource for resource in inventory}
    for resource in inventory:
        resource["metadata"]["uid"] = "offline-example-" + resource["metadata"]["name"]
    for section, kind in (("ircservers", "IRCServer"), ("weechats", "WeeChat"),
                          ("mcpclients", "MCPClient")):
        configured = config[section][0]
        assert configured["namespace"] == index[kind]["metadata"]["namespace"]
        assert configured["name"] == index[kind]["metadata"]["name"]
    # resources.json is a structural example, not this runtime's deployment plan.
    # These explicit changes demonstrate the required full DNS/port/path and
    # logical Connector-to-WeeChat identity link; no source file is mutated.
    index["IRCServer"]["spec"]["endpoint"] = "irc://irc.default.svc.cluster.local:6667"
    index["WeeChat"]["spec"]["mcp_endpoint"] = "http://weechat.default.svc.cluster.local:8080/mcp"
    connector = index["Connector"]["spec"]
    connector["weechatRef"] = {"name": "weechat"}
    connector["nick"] = index["WeeChat"]["spec"]["nick"]
    connector["mcp_endpoint"] = index["WeeChat"]["spec"]["mcp_endpoint"]
    index["MCPClient"]["spec"]["endpoint"] = "http://mcp.default.svc.cluster.local:8080/mcp"
    context = PlanContext("default", inventory)
    for kind in ("IRCServer", "WeeChat", "MCPClient"):
        router.bind_plan(CONTROLLERS[kind].build_plan(index[kind], context))


@pytest.mark.parametrize("source,target", [
    ("ircservers", "weechats"), ("ircservers", "mcpclients"), ("weechats", "mcpclients")])
def test_same_namespace_workload_names_cannot_collide_across_kinds(tmp_path, source, target):
    config = load_provider_config(str(EXAMPLE))
    source_key = "service_name" if source == "ircservers" else "workload_name"
    config[target][0]["workload_name"] = config[source][0][source_key]
    with pytest.raises(ValueError, match="^Invalid provider configuration file.$"):
        load_provider_config(write_config(tmp_path, config))


@pytest.mark.parametrize("section", ["ircservers", "weechats", "mcpclients"])
def test_same_kind_different_cr_cannot_claim_same_workload(tmp_path, section):
    config = load_provider_config(str(EXAMPLE))
    duplicate = deepcopy(config[section][0])
    duplicate["name"] = "second-resource"
    config[section].append(duplicate)
    with pytest.raises(ValueError, match="^Invalid provider configuration file.$"):
        load_provider_config(write_config(tmp_path, config))


def test_identical_service_names_in_distinct_namespaces_are_not_collisions(tmp_path):
    config = load_provider_config(str(EXAMPLE))
    duplicate = deepcopy(config["ircservers"][0])
    duplicate["namespace"] = "second-namespace"
    config["ircservers"].append(duplicate)
    assert load_provider_config(write_config(tmp_path, config)) == config


def test_example_does_not_claim_original_mcp_image_supports_bearer(tmp_path):
    config = load_provider_config(str(EXAMPLE))
    config["mcpclients"][0]["supports_bearer_file"] = False
    with pytest.raises(ValueError, match="^Invalid provider configuration file.$"):
        load_provider_config(write_config(tmp_path, config))
