"""Explicit opt-in runtime configuration; no workload provider by default.

The JSON file contains deployment choices and existing credential references,
never credentials. Loading it performs no Kubernetes, HTTP or Secret reads.
Installing RBAC and running this mode require separate operational approval.
"""
from dataclasses import fields
import json
import os
from pathlib import Path
import re

from .provider_router import ProviderRouter
from .providers import DisabledProvider


def _selected(entries, cls, *, secrets=False):
    if type(entries) is not list:
        raise ValueError()
    result = {}
    keys = {field.name for field in fields(cls)} | {"namespace", "name"}
    for entry in entries:
        if type(entry) is not dict or set(entry) != keys:
            raise ValueError()
        namespace, name = entry["namespace"], entry["name"]
        if (type(namespace) is not str or type(name) is not str or len(name) > 253
                or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace) is None
                or re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*", name) is None
                or (namespace, name) in result):
            raise ValueError()
        arguments = {key: value for key, value in entry.items() if key not in {"namespace", "name"}}
        if secrets:
            from .mcp_workload_provider import SecretKeyRef

            def reference(value):
                if type(value) is not dict or set(value) != {"name", "key"}:
                    raise ValueError()
                return SecretKeyRef(**value)

            if "auth_secret" in arguments:
                arguments["auth_secret"] = reference(arguments["auth_secret"])
            if "secret_profiles" in arguments:
                if type(arguments["secret_profiles"]) is not dict:
                    raise ValueError()
                arguments["secret_profiles"] = {key: reference(value) for key, value in arguments["secret_profiles"].items()}
        result[(namespace, name)] = cls(**arguments)
    return result


def _parse(config):
    from .durable_provider import DurableRuntimeSettings
    from .irc_provider import IRCServerSettings
    from .mcp_workload_provider import MCPClientSettings, WeeChatSettings, _validate_settings

    allowed = {"version", "ircservers", "weechats", "mcpclients", "durable", "readiness"}
    if (type(config) is not dict or set(config) - allowed or not {"version", "ircservers"}.issubset(config)
            or type(config["version"]) is not int or config["version"] != 1):
        raise ValueError()
    irc = _selected(config["ircservers"], IRCServerSettings)
    weechat = _selected(config.get("weechats", []), WeeChatSettings, secrets=True)
    mcp = _selected(config.get("mcpclients", []), MCPClientSettings, secrets=True)
    occupied = set()
    for namespace_name, settings in [*irc.items(), *weechat.items(), *mcp.items()]:
        if isinstance(settings, IRCServerSettings):
            settings.validate()
            workload = settings.service_name
        else:
            _validate_settings(settings)
            workload = settings.workload_name
        target = namespace_name[0], workload
        if target in occupied:
            raise ValueError()
        occupied.add(target)
    durable = None
    if "durable" in config:
        value = config["durable"]
        if type(value) is not dict or set(value) != {"endpoint", "token_file"}:
            raise ValueError()
        durable = DurableRuntimeSettings(**value)
        durable.validate()
    from .mcp_readiness import MCPReadinessSettings
    readiness = {}
    entries = config.get("readiness", [])
    if type(entries) is not list:
        raise ValueError()
    allowed_fields = {field.name for field in fields(MCPReadinessSettings)}
    for entry in entries:
        if (type(entry) is not dict or "endpoint" not in entry
                or set(entry) - allowed_fields - {"endpoint"}):
            raise ValueError()
        endpoint = entry["endpoint"]
        if type(endpoint) is not str or endpoint in readiness:
            raise ValueError()
        setting = MCPReadinessSettings(**{key: value for key, value in entry.items() if key != "endpoint"})
        setting.validate(endpoint)
        readiness[endpoint] = setting
    return irc, weechat, mcp, durable, readiness


def load_provider_config(path: str | None = None):
    path = path if path is not None else os.environ.get("CUSE_PROVIDER_CONFIG")
    if not path:
        return None
    try:
        config = json.loads(Path(path).read_text())
        _parse(config)
        return config
    except Exception:
        raise ValueError("Invalid provider configuration file.") from None


def build_provider(config, *, apps, core, readiness=None, control_transport=None):
    if config is None:
        return DisabledProvider()
    from .bot_provider import BotBindingProvider
    from .connector_provider import WeeChatConnectorProvider
    from .durable_provider import DurableRuntimeProvider
    from .irc_provider import IRCServerKubernetesProvider
    from .mcp_workload_provider import MCPClientKubernetesProvider, WeeChatKubernetesProvider

    irc, weechat, mcp, durable, probes = _parse(config)
    if readiness is None and probes:
        from .mcp_readiness import AiohttpMCPTransport, MCPReadinessAdapter
        readiness = MCPReadinessAdapter(settings=probes, transport=AiohttpMCPTransport())
    router = ProviderRouter({
        "IRCServer": IRCServerKubernetesProvider(apps=apps, core=core, settings=irc),
        "WeeChat": WeeChatKubernetesProvider(apps=apps, core=core, settings=weechat, readiness=readiness),
        "MCPClient": MCPClientKubernetesProvider(apps=apps, core=core, settings=mcp, readiness=readiness),
    })
    if durable is not None:
        provider = DurableRuntimeProvider(durable, transport=control_transport)
        router.providers.update(PiHarness=provider, Session=provider)
    router.providers["Connector"] = WeeChatConnectorProvider(router)
    router.providers["Bot"] = BotBindingProvider(router)
    return router
