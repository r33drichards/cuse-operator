"""Opt-in, retained Kubernetes workloads for WeeChat MCP and mcp-js.

Verified source contracts:
- r33drichards/weechat-mcp @ 7d934730d6b6864a937035201f6caa039b080792,
  Dockerfile, container_entrypoint.py, bridge.py: one bundled runtime, UID 10001,
  HTTP /mcp with PORT/MCP_HOST/MCP_ALLOWED_HOSTS and bearer MCP_AUTH_TOKEN.
- r33drichards/mcp-js @ 8d1d4f9d568e395b8e3f8dfe5cb17b8ef58ff43b,
  Dockerfile/config/CLI: mcp-v8 entrypoint, UID 1000; renderer supplies arguments.
  Downstream bearer token_file is an additional, explicitly asserted image
  capability. The pinned original revision does not provide that capability.

Images, service DNS, ports, allowed hosts, storage mode, policies, and Secret
references must be explicitly supplied. Only ephemeral profiles/storage and
internal ClusterIP HTTP endpoints are supported. A ClusterIP is NOT network
isolation. mcp-js's inbound endpoint is explicitly unauthenticated in this subset;
its downstream bearer credentials do not authenticate inbound requests. External
TLS, ingress, NetworkPolicy, persistence and a credential issuer are out of scope.

Existing same-namespace Secrets are referenced by pods only. No Secret is read,
created, logged or copied into a spec/ConfigMap by the operator. Rotation of a
WeeChat env Secret needs an explicit pod rollout; changing the Secret's contents
alone cannot be observed here. mcp-js mounted files refresh through Kubernetes
projection, but the runtime reads them on connection/reconnection.

ConfigMaps, Deployments and Services have complete CR-UID annotations, no owner
references, no adoption and no deletion. Updates are UID/resourceVersion-tested
JSON Patches. A ConfigMap or TCP listener never proves MCP readiness: an injected
protocol adapter is an additional mandatory gate after matching rollout/backend
checks. No adapter => ready=False. Its observe method must return an object with
ready=True only for supported protocol/authentication/IRC evidence; it must not
obtain credentials by adding operator Secret-read access. No network client is
constructed or called on import. Connector is a logical export, never a workload.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from aiohttp import ClientError
from kubernetes_asyncio.client.exceptions import ApiException

from .irc_provider import (
    IRCServerKubernetesProvider, _DNS_RE, _IMAGE_RE, _LABEL_RE, _digest,
    _identity_values, _json, _matches, _wire,
)
from .mcp_config import McpConfigError, _authority, render_mcp_config
from .providers import (
    DesiredPlan, Observation, ProviderNotConfigured, ResourceIdentity,
    RetryableProviderError, TerminalProviderError, UncertainOutcome,
)
from .resources import ResourceSpecError, validate_resource_spec

_OWNER = "cuse.io/workload-owner"
_LABEL = "cuse.io/workload-owner-key"
_REVISION = "cuse.io/workload-revision"
_KEY = "cuse.io/workload-idempotency-key"
_RUNTIME = "cuse.io/runtime-digest"
_CONFIG_DIR = "/etc/mcp-v8"
_SECRET_ROOT = "/var/run/secrets/cuse-mcp"


@dataclass(frozen=True)
class SecretKeyRef:
    """Reference only: namespace is necessarily the workload's own namespace."""
    name: str
    key: str

    def validate(self) -> None:
        if (type(self.name) is not str or len(self.name) > 253
                or not re.fullmatch(_DNS_RE, self.name)
                or type(self.key) is not str or len(self.key) > 253
                or not re.fullmatch(r"[A-Za-z0-9._-]+", self.key)
                or self.key in {".", ".."}):
            raise TerminalProviderError("Invalid same-namespace Secret reference.")


@dataclass(frozen=True)
class WeeChatSettings:
    image: str
    workload_name: str
    cluster_domain: str
    http_port: int
    bind_host: str
    allowed_hosts: tuple[str, ...]
    channels: tuple[str, ...]
    storage_mode: str
    full_control: bool
    auth_secret: SecretKeyRef


@dataclass(frozen=True)
class MCPClientSettings:
    image: str
    workload_name: str
    cluster_domain: str
    http_port: int
    bind_host: str
    allowed_hosts: tuple[str, ...]
    storage_mode: str
    endpoint_mode: str
    policies: dict
    secret_profiles: Mapping[str, SecretKeyRef]
    supports_bearer_file: bool


def _validate_settings(config: WeeChatSettings | MCPClientSettings) -> None:
    if (type(config.image) is not str or not re.fullmatch(_IMAGE_RE, config.image)
            or type(config.workload_name) is not str
            or not re.fullmatch(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?", config.workload_name)
            or type(config.cluster_domain) is not str or len(config.cluster_domain) > 253
            or not re.fullmatch(_DNS_RE, config.cluster_domain)
            or type(config.http_port) is not int or not 1024 <= config.http_port <= 65535
            or config.bind_host != "0.0.0.0" or config.storage_mode != "ephemeral"
            or type(config.allowed_hosts) not in (tuple, list) or not config.allowed_hosts):
        raise TerminalProviderError("Unsupported MCP workload settings.")
    try:
        for host in config.allowed_hosts:
            _authority(host)
    except (McpConfigError, TypeError, ValueError):
        raise TerminalProviderError("Explicit MCP host allowlist is required.") from None
    if isinstance(config, WeeChatSettings):
        if (type(config.full_control) is not bool
                or type(config.channels) not in (tuple, list) or not 1 <= len(config.channels) <= 20
                or any(type(c) is not str or not re.fullmatch(r"#[A-Za-z0-9_.-]{1,100}", c)
                       for c in config.channels)
                or len(config.channels) != len(set(config.channels))
                or not isinstance(config.auth_secret, SecretKeyRef)):
            raise TerminalProviderError("Explicit WeeChat channels, policy and Secret are required.")
        config.auth_secret.validate()
    else:
        if (config.endpoint_mode != "internal-unauthenticated"
                or type(config.supports_bearer_file) is not bool
                or not isinstance(config.secret_profiles, Mapping)):
            raise TerminalProviderError("Unsupported MCPClient runtime settings.")
        if config.secret_profiles and not config.supports_bearer_file:
            raise ProviderNotConfigured("The selected image must support bearer token files.")
        for name, ref in config.secret_profiles.items():
            if type(name) is not str or not isinstance(ref, SecretKeyRef):
                raise TerminalProviderError("Invalid downstream Secret profile.")
            ref.validate()


class _RetainedMCPWorkload:
    kind: str
    settings_type: type

    def __init__(self, *, apps: Any, core: Any, settings: Mapping, readiness: Any = None):
        self.apps, self.core = apps, core
        self.settings = deepcopy(dict(settings))
        self.readiness = readiness
        self._plans: dict[ResourceIdentity, DesiredPlan] = {}

    def _config(self, identity: ResourceIdentity) -> WeeChatSettings | MCPClientSettings:
        if (identity.kind != self.kind or type(identity.namespace) is not str
                or not re.fullmatch(_LABEL_RE, identity.namespace)
                or type(identity.name) is not str or len(identity.name) > 253
                or not re.fullmatch(_DNS_RE, identity.name)
                or type(identity.uid) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identity.uid)):
            raise TerminalProviderError("Invalid MCP workload resource identity.")
        config = self.settings.get((identity.namespace, identity.name))
        if config is None or self.apps is None or self.core is None:
            raise ProviderNotConfigured("MCP workload settings or clients are missing.")
        if not isinstance(config, self.settings_type):
            raise TerminalProviderError("Wrong MCP workload settings type.")
        _validate_settings(config)
        return config

    def bind_plan(self, desired: DesiredPlan) -> None:
        """Pure validation; must also be called after an operator restart."""
        self._manifests(desired)
        self._plans[desired.identity] = deepcopy(desired)

    def _validate_plan(self, desired: DesiredPlan, config: Any) -> str:
        identity, intent = desired.identity, desired.intent
        try:
            validate_resource_spec(self.kind, intent["spec"])
            if (intent["kind"] != self.kind or desired.revision != _digest(intent)
                    or desired.idempotency_key != _digest(_identity_values(identity) + [desired.revision])):
                raise ValueError
            host = f"{config.workload_name}.{identity.namespace}.svc.{config.cluster_domain}"
            endpoint = f"http://{host}:{config.http_port}/mcp"
            hosts = {_authority(value) for value in config.allowed_hosts}
            if len(host) > 253 or not ({host, f"{host}:{config.http_port}"} & hosts):
                raise ValueError
            spec = intent["spec"]
            if self.kind == "WeeChat":
                if (set(intent) != {"kind", "spec", "client", "server", "dependencies"}
                        or intent["client"] != {"nick": spec["nick"], "mcpEndpoint": spec["mcp_endpoint"]}
                        or spec["mcp_endpoint"] != endpoint
                        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,29}", spec["nick"])):
                    raise ValueError
                validate_resource_spec("IRCServer", intent["server"])
                refs = [("IRCServer", spec["serverRef"])]
            else:
                refs = [("Connector", ref) for ref in spec.get("connectorRefs", [])]
                if (set(intent) != {"kind", "spec", "endpoint", "downstreams", "dependencies"}
                        or intent["endpoint"] != spec["endpoint"] or spec["endpoint"] != endpoint
                        or type(intent["downstreams"]) is not list
                        or len(intent["downstreams"]) != len(refs)):
                    raise ValueError
            dependencies = intent["dependencies"]
            if type(dependencies) is not list or len(dependencies) != len(refs):
                raise ValueError
            if self.kind == "WeeChat" and dependencies[0]["specDigest"] != _digest(intent["server"]):
                raise ValueError
            for dependency, (kind, ref) in zip(dependencies, refs):
                if (type(dependency) is not dict or set(dependency) != {
                        "kind", "namespace", "name", "uid", "specDigest", "desiredRevision"}
                        or dependency["kind"] != kind or dependency["namespace"] != identity.namespace
                        or dependency["name"] != ref["name"]
                        or not isinstance(dependency["uid"], str) or not dependency["uid"]
                        or ref.get("uid", dependency["uid"]) != dependency["uid"]
                        or any(type(dependency[key]) is not str
                               or not re.fullmatch(r"[0-9a-f]{64}", dependency[key])
                               for key in ("specDigest", "desiredRevision"))):
                    raise ValueError
        except (KeyError, TypeError, ValueError, ResourceSpecError, McpConfigError):
            raise TerminalProviderError("Unsupported MCP desired intent or service endpoint.") from None
        return endpoint

    def _manifests(self, desired: DesiredPlan) -> tuple[dict, dict, dict]:
        config = self._config(desired.identity)
        self._validate_plan(desired, config)
        data, args, env, mounts, volumes, uid, render_digest = self._runtime(desired, config)
        labels = {_LABEL: _digest(_identity_values(desired.identity))[:32]}
        runtime_digest = _digest([data, args, env, mounts, volumes, config.image, render_digest])
        annotations = {_OWNER: _json(_identity_values(desired.identity)),
                       _REVISION: desired.revision, _KEY: desired.idempotency_key,
                       _RUNTIME: runtime_digest}
        metadata = {"name": config.workload_name, "namespace": desired.identity.namespace,
                    "labels": labels, "annotations": annotations}
        configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": deepcopy(metadata),
                     "data": data, "binaryData": {}}
        probe = {"tcpSocket": {"port": "mcp", "host": ""}, "periodSeconds": 5,
                 "timeoutSeconds": 2, "failureThreshold": 3, "successThreshold": 1}
        container = {
            "name": "runtime", "image": config.image, "imagePullPolicy": "IfNotPresent",
            "command": [], "args": args, "envFrom": [], "env": env,
            "volumeMounts": mounts + [{"name": "tmp", "mountPath": "/tmp", "readOnly": False}],
            "ports": [{"name": "mcp", "containerPort": config.http_port,
                       "protocol": "TCP", "hostPort": 0, "hostIP": ""}],
            "lifecycle": None, "livenessProbe": None, "readinessProbe": deepcopy(probe),
            "startupProbe": {**deepcopy(probe), "failureThreshold": 60},
            "securityContext": {"allowPrivilegeEscalation": False, "privileged": False,
                                "readOnlyRootFilesystem": True, "runAsNonRoot": True,
                                "runAsUser": uid, "runAsGroup": uid,
                                "procMount": None, "seLinuxOptions": None,
                                "windowsOptions": None, "appArmorProfile": None,
                                "seccompProfile": {"type": "RuntimeDefault"},
                                "capabilities": {"drop": ["ALL"], "add": []}},
        }
        deployment = {
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": deepcopy(metadata),
            "spec": {"replicas": 1, "paused": False, "strategy": {"type": "Recreate"},
                     "selector": {"matchLabels": labels, "matchExpressions": []},
                     "template": {"metadata": {"labels": labels, "annotations": annotations},
                                  "spec": {"automountServiceAccountToken": False,
                                           "enableServiceLinks": False, "hostNetwork": False,
                                           "hostPID": False, "hostIPC": False,
                                           "initContainers": [],
                                           "volumes": volumes + [{"name": "tmp", "emptyDir": {}}],
                                           "securityContext": {"runAsNonRoot": True,
                                                               "runAsUser": uid, "runAsGroup": uid,
                                                               "fsGroup": uid,
                                                               "supplementalGroups": [], "sysctls": [],
                                                               "seLinuxOptions": None,
                                                               "windowsOptions": None,
                                                               "appArmorProfile": None,
                                                               "seccompProfile": {"type": "RuntimeDefault"}},
                                           "containers": [container]}}},
        }
        service = {"apiVersion": "v1", "kind": "Service", "metadata": deepcopy(metadata),
                   "spec": {"type": "ClusterIP", "selector": labels, "externalIPs": [],
                            "publishNotReadyAddresses": False,
                            "ports": [{"name": "mcp", "port": config.http_port,
                                       "targetPort": "mcp", "protocol": "TCP"}]}}
        return configmap, deployment, service

    async def _call(self, method: Any, *, write: bool = False, **kwargs: Any) -> Any:
        kwargs.setdefault("_request_timeout", 30)
        try:
            return _wire(await method(**kwargs))
        except ApiException as error:
            if error.status == 404 and not write:
                return None
            if (error.status == 422 and write
                    and kwargs.get("_content_type") == "application/json-patch+json"):
                raise RetryableProviderError("MCP conditional patch requires re-observation.") from None
            if error.status in {400, 401, 403, 405, 422}:
                raise TerminalProviderError("Kubernetes rejected the MCP workload request.") from None
            if error.status in {404, 409, 429}:
                raise RetryableProviderError("MCP workload requires re-observation.") from None
            error_type = UncertainOutcome if write else RetryableProviderError
            raise error_type("MCP Kubernetes request did not complete reliably.") from None
        except (TimeoutError, OSError, ClientError):
            error_type = UncertainOutcome if write else RetryableProviderError
            raise error_type("MCP Kubernetes request did not complete reliably.") from None

    async def _read_objects(self, identity: ResourceIdentity) -> tuple[Any, Any, Any]:
        config = self._config(identity)
        args = {"name": config.workload_name, "namespace": identity.namespace}
        objects = []
        for read in (self.core.read_namespaced_config_map, self.apps.read_namespaced_deployment,
                     self.core.read_namespaced_service):
            actual = await self._call(read, **args)
            if actual is not None:
                meta = actual.get("metadata", {})
                if (meta.get("namespace") != identity.namespace
                        or meta.get("name") != config.workload_name
                        or meta.get("annotations", {}).get(_OWNER) != _json(_identity_values(identity))
                        or not meta.get("uid") or not meta.get("resourceVersion")
                        or meta.get("ownerReferences") or meta.get("deletionTimestamp")):
                    raise TerminalProviderError("MCP workload ownership is conflicting or terminating.")
            objects.append(actual)
        return tuple(objects)

    @staticmethod
    def _converged(actual: dict, expected: dict) -> bool:
        if expected["kind"] == "ConfigMap":
            return (_matches(actual, expected) and actual.get("data", {}) == expected["data"]
                    and not actual.get("binaryData") and not actual.get("immutable", False))
        return IRCServerKubernetesProvider._converged(actual, expected)

    async def observe(self, identity: ResourceIdentity) -> Observation:
        self._config(identity)
        desired = self._plans.get(identity)
        if desired is None:
            raise ProviderNotConfigured("Bind the current MCP workload plan before observation.")
        expected = self._manifests(desired)
        actual = await self._read_objects(identity)
        if all(obj is None for obj in actual):
            return Observation()
        if any(obj is None or not self._converged(obj, body) for obj, body in zip(actual, expected)):
            return Observation(exists=True)
        _, deployment, service = actual
        ready = (IRCServerKubernetesProvider._deployment_ready(deployment)
                 and bool(service["spec"].get("clusterIP")))
        if ready:
            ready = await self._backends_ready(identity, expected[1]["spec"]["template"])
        # A TCP probe is merely startup/backend evidence, never an MCP auth check.
        if ready:
            ready = False
            if self.readiness is not None:
                endpoint = desired.intent["client"]["mcpEndpoint"] if self.kind == "WeeChat" else desired.intent["endpoint"]
                result = await self.readiness.observe(
                    endpoint=endpoint, provider=self.kind,
                    expected_server=desired.intent["server"]["endpoint"] if self.kind == "WeeChat" else None,
                    expected_nick=desired.intent["spec"]["nick"] if self.kind == "WeeChat" else None)
                ready = getattr(result, "ready", False) is True
        return Observation(True, desired.revision, ready)

    async def _backends_ready(self, identity: ResourceIdentity, template: dict) -> bool:
        config = self._config(identity)
        endpoints = await self._call(self.core.read_namespaced_endpoints,
                                     name=config.workload_name, namespace=identity.namespace)
        if not endpoints or endpoints.get("metadata", {}).get("deletionTimestamp"):
            return False
        found = False
        for subset in endpoints.get("subsets", []):
            if (subset.get("notReadyAddresses") or not subset.get("addresses")
                    or not _matches(subset.get("ports"), [
                        {"name": "mcp", "port": config.http_port, "protocol": "TCP"}])):
                return False
            for address in subset["addresses"]:
                target = address.get("targetRef", {})
                if (target.get("kind") != "Pod" or target.get("namespace") != identity.namespace
                        or not target.get("name") or not target.get("uid")):
                    return False
                pod = await self._call(self.core.read_namespaced_pod,
                                       name=target["name"], namespace=identity.namespace)
                if not pod:
                    return False
                meta, status = pod.get("metadata", {}), pod.get("status", {})
                pod_ips = {item.get("ip") for item in status.get("podIPs", [])}
                pod_ips.add(status.get("podIP"))
                if (meta.get("uid") != target["uid"] or meta.get("deletionTimestamp")
                        or meta.get("namespace") != identity.namespace or not _matches(pod, template)
                        or not address.get("ip") or address["ip"] not in pod_ips
                        or status.get("phase") != "Running"
                        or not any(c.get("type") == "Ready" and c.get("status") == "True"
                                   for c in status.get("conditions", []))):
                    return False
                found = True
        return found

    async def ensure(self, desired: DesiredPlan, *, idempotency_key: str) -> None:
        if idempotency_key != desired.idempotency_key:
            raise TerminalProviderError("MCP workload idempotency key differs from desired intent.")
        expected = self._manifests(desired)
        self.bind_plan(desired)
        actual = await self._read_objects(desired.identity)
        # Check every collision and immutable condition before the first write.
        if actual[0] is not None and actual[0].get("immutable", False):
            raise TerminalProviderError("MCP ConfigMap is immutable and cannot be managed.")
        if actual[1] is not None:
            selector = actual[1].get("spec", {}).get("selector", {})
            if (selector.get("matchLabels") != expected[1]["spec"]["selector"]["matchLabels"]
                    or selector.get("matchExpressions")):
                raise TerminalProviderError("MCP Deployment selector is immutable and conflicting.")
        if actual[2] is not None and actual[2].get("spec", {}).get("clusterIP") == "None":
            raise TerminalProviderError("MCP Service requires an allocated ClusterIP.")
        for old, body, create, patch in zip(actual, expected,
                (self.core.create_namespaced_config_map, self.apps.create_namespaced_deployment,
                 self.core.create_namespaced_service),
                (self.core.patch_namespaced_config_map, self.apps.patch_namespaced_deployment,
                 self.core.patch_namespaced_service)):
            if old is None:
                await self._call(create, write=True, namespace=desired.identity.namespace, body=body)
            elif not self._converged(old, body):
                changes = IRCServerKubernetesProvider._patch(old, body)
                if body["kind"] == "ConfigMap":
                    # Config data is atomic: remove extra keys, including env injections.
                    for key in ("data", "binaryData"):
                        if old.get(key, {}) != body[key]:
                            changes.append({"op": "add", "path": "/" + key,
                                            "value": deepcopy(body[key])})
                await self._call(patch, write=True, namespace=desired.identity.namespace,
                                 name=body["metadata"]["name"], body=changes,
                                 _content_type="application/json-patch+json")


class WeeChatKubernetesProvider(_RetainedMCPWorkload):
    kind = "WeeChat"
    settings_type = WeeChatSettings

    @staticmethod
    def _runtime(desired: DesiredPlan, config: WeeChatSettings):
        try:
            endpoint = urlsplit(desired.intent["server"]["endpoint"])
            if (endpoint.scheme not in {"irc", "ircs"} or endpoint.port is None
                    or not 1 <= endpoint.port <= 65535
                    or endpoint.username is not None or endpoint.password is not None
                    or endpoint.path or endpoint.query or endpoint.fragment
                    or endpoint.hostname is None
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", endpoint.hostname)):
                raise ValueError
        except (ValueError, TypeError):
            raise TerminalProviderError("WeeChat requires an explicit unauthenticated IRC endpoint.") from None
        data = {
            "IRC_HOST": endpoint.hostname, "IRC_PORT": str(endpoint.port),
            "IRC_TLS": "true" if endpoint.scheme == "ircs" else "false",
            "IRC_NICK": desired.intent["spec"]["nick"], "IRC_CHANNELS": ",".join(config.channels),
            "MCP_TRANSPORT": "http", "MCP_AUTH_MODE": "bearer", "MCP_HOST": config.bind_host,
            "PORT": str(config.http_port),
            "MCP_ALLOWED_HOSTS": ",".join(sorted({_authority(h) for h in config.allowed_hosts})),
            "WEECHAT_MCP_FULL_CONTROL": "1" if config.full_control else "0",
            # Empty explicitly selects source's ephemeral mode; saved config must never win.
            "WEECHAT_HOME": "", "MCP_EVENTS_DB": "", "TMPDIR": "/tmp",
        }
        env = [{"name": key, "valueFrom": {"configMapKeyRef": {
            "name": config.workload_name, "key": key, "optional": False}}} for key in sorted(data)]
        env.append({"name": "MCP_AUTH_TOKEN", "valueFrom": {"secretKeyRef": {
            "name": config.auth_secret.name, "key": config.auth_secret.key, "optional": False}}})
        return data, [], env, [], [], 10001, None


class MCPClientKubernetesProvider(_RetainedMCPWorkload):
    kind = "MCPClient"
    settings_type = MCPClientSettings

    @staticmethod
    def _runtime(desired: DesiredPlan, config: MCPClientSettings):
        paths, mounts, volumes = {}, [], []
        for index, (name, ref) in enumerate(sorted(config.secret_profiles.items())):
            volume = f"downstream-{index}"
            directory = f"{_SECRET_ROOT}/{volume}"
            paths[name] = directory + "/token"
            mounts.append({"name": volume, "mountPath": directory, "readOnly": True})
            volumes.append({"name": volume, "secret": {
                "secretName": ref.name, "optional": False, "defaultMode": 0o440,
                "items": [{"key": ref.key, "path": "token"}]}})
        try:
            rendered = render_mcp_config(
                desired, bind_host=config.bind_host, http_port=config.http_port,
                allowed_hosts=config.allowed_hosts, policies=config.policies,
                config_path=_CONFIG_DIR + "/server.json", bearer_token_files=paths)
        except (McpConfigError, TypeError, ValueError):
            raise TerminalProviderError("Unsupported MCPClient configuration or Secret profile.") from None
        data = {"server.json": rendered.config_json}
        for path, policy in rendered.policy_files:
            # The renderer emits siblings of the fixed config path, not arbitrary mounts.
            if str(PurePosixPath(path).parent) != _CONFIG_DIR:
                raise TerminalProviderError("Unsupported MCP policy artifact path.")
            data[PurePosixPath(path).name] = policy
        mounts.insert(0, {"name": "config", "mountPath": _CONFIG_DIR, "readOnly": True})
        volumes.insert(0, {"name": "config", "configMap": {
            "name": config.workload_name, "optional": False, "defaultMode": 0o444,
            "items": [{"key": key, "path": key} for key in sorted(data)]}})
        env = [{"name": "MCP_V8_ALLOWED_HOSTS", "value": ",".join(sorted({
            _authority(h) for h in config.allowed_hosts}))},
            {"name": "PORT", "value": str(config.http_port)}, {"name": "TMPDIR", "value": "/tmp"}]
        return data, list(rendered.args), env, mounts, volumes, 1000, rendered.digest
