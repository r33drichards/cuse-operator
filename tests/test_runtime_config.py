import json

import pytest

from cuse_operator.providers import DisabledProvider
from cuse_operator.runtime import build_provider, load_provider_config


def test_default_does_not_enable_effects(monkeypatch):
    monkeypatch.delenv("CUSE_PROVIDER_CONFIG", raising=False)
    assert load_provider_config() is None
    assert isinstance(build_provider(None, apps=None, core=None), DisabledProvider)


@pytest.mark.parametrize("value", [None, [], {}, {"version": True, "ircservers": []},
                                    {"version": 1, "ircservers": [], "secret": "hidden"},
                                    {"version": 1, "ircservers": [{"name": "missing-fields"}]}])
def test_bad_config_is_redacted(tmp_path, value):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="^Invalid provider configuration file.$"):
        load_provider_config(str(path))


def test_explicit_empty_config_and_missing_file(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text('{"version":1,"ircservers":[]}')
    assert load_provider_config(str(path)) == {"version": 1, "ircservers": []}
    with pytest.raises(ValueError, match="^Invalid provider configuration file.$"):
        load_provider_config(str(tmp_path / "missing"))


def valid_entry():
    return {"namespace": "team", "name": "irc", "image": "registry.example/irc@sha256:" + "a" * 64,
            "service_name": "irc", "cluster_domain": "cluster.local", "network_name": "TestNet",
            "endpoint_mode": "internal-plaintext"}


def test_explicit_provider_construction_has_no_io(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"version": 1, "ircservers": [valid_entry()]}))
    # Opaque sentinels cannot make API calls, and none occur at construction.
    provider = build_provider(load_provider_config(str(path)), apps=object(), core=object())
    assert set(provider.providers) == {"IRCServer", "WeeChat", "MCPClient", "Connector", "Bot"}


def test_duplicate_target_config_rejected(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"version": 1, "ircservers": [valid_entry(), valid_entry()]}))
    with pytest.raises(ValueError, match="^Invalid provider configuration file.$"):
        load_provider_config(str(path))


def test_optional_rbac_has_no_credential_or_delete_access():
    from pathlib import Path
    import yaml

    document = yaml.safe_load((Path(__file__).parents[1] / "config/rbac-irc-provider.yaml").read_text())
    assert document["kind"] == "Role"
    rules = document["rules"]
    assert {resource for rule in rules for resource in rule["resources"]} == {"deployments", "services", "pods", "endpoints"}
    assert all("delete" not in rule["verbs"] and "*" not in rule["verbs"] for rule in rules)


def test_readiness_and_durable_configuration_construct_without_io(tmp_path):
    config = {"version": 1, "ircservers": [],
              "durable": {"endpoint": "https://runtime.example", "token_file": "/mount/control-token"},
              "readiness": [{"endpoint": "https://mcp.example/mcp", "provider": "WeeChat",
                             "auth_mode": "bearer", "token_file": "/mount/probe-token"}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    router = build_provider(load_provider_config(str(path)), apps=object(), core=object())
    assert {"PiHarness", "Session"} <= router.providers.keys()


@pytest.mark.parametrize("entries", [None, [{}], [
    {"endpoint": "https://mcp.example/mcp", "provider": "WeeChat", "auth_mode": "bearer",
     "token_file": "/mount/token", "unexpected": True}], [
    {"endpoint": "https://mcp.example/mcp", "provider": "MCPClient", "auth_mode": "internal-unauthenticated"}
    ] * 2])
def test_invalid_readiness_configuration_rejected(tmp_path, entries):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "ircservers": [], "readiness": entries}))
    with pytest.raises(ValueError, match="^Invalid provider configuration file.$"):
        load_provider_config(str(path))


def test_workload_rbac_does_not_read_secrets_or_delete():
    from pathlib import Path
    import yaml
    doc = yaml.safe_load((Path(__file__).parents[1] / "config/rbac-workload-providers.yaml").read_text())
    resources = {resource for rule in doc["rules"] for resource in rule["resources"]}
    assert resources == {"deployments", "configmaps", "services", "pods", "endpoints"}
    assert all(set(rule["verbs"]) <= {"get", "create", "patch"} for rule in doc["rules"])
