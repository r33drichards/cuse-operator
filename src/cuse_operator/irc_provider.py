"""Opt-in Kubernetes adapter for the r33drichards/docker IRC server contract.

Source inspected at 92cd2c5884d3b695c59de5325b7e3ce1e07838fb:
https://github.com/r33drichards/docker/tree/92cd2c5884d3b695c59de5325b7e3ce1e07838fb
Dockerfile: UID/GID 10000, entrypoint /entrypoint.sh, plaintext TCP 6667.
entrypoint.sh: writable /inspircd/conf; generates self-signed TLS certificates.
conf/config.sh: INSP_SERVER_NAME / INSP_NET_NAME / INSP_ENABLE_DNSBL.
conf/inspircd.conf: plaintext listener 6667; optional opers are not enabled here.
The fork's workflow only publishes from inspircd/docker, so no image is guessed.

This adapter only supports explicitly selected, unauthenticated internal-plaintext
ClusterIP service. It does not provide TLS, network isolation, external DNS, IRC
login checks, persistence, or high availability. Readiness means Kubernetes has
observed one healthy deployment replica and its selected, ready TCP backend.
Bind the current plan before observing (including after an operator restart).
Injected clients are kubernetes_asyncio AppsV1Api and CoreV1Api or test doubles.
No client construction, credential loading, delete API, or ownerReferences here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

from aiohttp import ClientError
from kubernetes_asyncio.client.exceptions import ApiException

from .providers import (
    DesiredPlan, Observation, ProviderNotConfigured, ResourceIdentity,
    RetryableProviderError, TerminalProviderError, UncertainOutcome,
)
from .resources import ResourceSpecError, validate_resource_spec

_OWNER = "cuse.io/irc-owner"
_LABEL = "cuse.io/irc-owner-key"
_REVISION = "cuse.io/irc-revision"
_KEY = "cuse.io/irc-idempotency-key"
_LABEL_RE = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
_DNS_RE = rf"{_LABEL_RE}(?:\.{_LABEL_RE})*"
_IMAGE_RE = r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}"
# Non-pointer API booleans may disappear from JSON when false. Do not treat
# missing automountServiceAccountToken/allowPrivilegeEscalation as false.
_ABSENT_DEFAULTS = {**dict.fromkeys(("paused", "hostNetwork", "hostPID", "hostIPC",
                                    "publishNotReadyAddresses", "privileged",
                                    "readOnlyRootFilesystem"), False),
                    "hostPort": 0, "hostIP": "", "host": ""}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identity_values(identity: ResourceIdentity) -> list[str]:
    return [identity.kind, identity.namespace, identity.name, identity.uid]


def _wire(value: Any) -> Any:
    """Normalize dictionaries or official Kubernetes models to API JSON names."""
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items() if item is not None}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    if hasattr(value, "attribute_map"):
        return {wire_name: _wire(getattr(value, attribute))
                for attribute, wire_name in value.attribute_map.items()
                if getattr(value, attribute) is not None}
    return value


def _matches(actual: Any, expected: Any) -> bool:
    """Compare the explicit managed projection, allowing unlisted dictionary keys.

    This is not full-object equality: API defaults, scheduler assignments and
    unrelated metadata survive. Execution/security-sensitive overrides that this
    adapter governs must be explicit in the desired projection (including null
    for prohibited optional fields). Lists are atomic; omitted empty values and
    selected Kubernetes scalar defaults are normalized.
    """
    if actual is None and expected in ([], {}):
        return True
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            _matches(actual.get(key, _ABSENT_DEFAULTS.get(key)), value)
            for key, value in expected.items())
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_matches(a, e) for a, e in zip(actual, expected)))
    return actual == expected


@dataclass(frozen=True)
class IRCServerSettings:
    """Every deployment choice is explicit; an immutable fork image is required.

    image must be a verified build of the documented fork, pinned by sha256.
    Settings keys are (namespace, IRCServer resource name), not the domain id.
    service_name is also the Deployment name and is retained after CR deletion.
    """
    image: str
    service_name: str
    cluster_domain: str
    network_name: str
    endpoint_mode: str

    def validate(self) -> None:
        if any(not isinstance(value, str) or not value for value in (
                self.image, self.service_name, self.cluster_domain,
                self.network_name, self.endpoint_mode)):
            raise ProviderNotConfigured("IRC provider settings are incomplete.")
        if (self.endpoint_mode != "internal-plaintext"
                or not re.fullmatch(_IMAGE_RE, self.image)
                or not re.fullmatch(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?", self.service_name)
                or len(self.cluster_domain) > 253
                or not re.fullmatch(_DNS_RE, self.cluster_domain)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.network_name)):
            raise TerminalProviderError("Unsupported IRC provider settings.")


class IRCServerKubernetesProvider:
    """Reconcile a retained Deployment and ClusterIP Service, with no deletion.

    ensure re-reads both objects before making any write, checks complete CR UID
    ownership, and uses optimistic JSON Patch UID/resourceVersion tests. A create
    conflict is retried by re-observation, never adopted. Unknown write outcomes
    produce UncertainOutcome. Deterministic names make retries restart-safe.
    """
    def __init__(self, *, apps: Any, core: Any,
                 settings: Mapping[tuple[str, str], IRCServerSettings]):
        self.apps = apps
        self.core = core
        self.settings = dict(settings)
        self._plans: dict[ResourceIdentity, DesiredPlan] = {}

    def _config(self, identity: ResourceIdentity) -> IRCServerSettings:
        if (identity.kind != "IRCServer"
                or not isinstance(identity.namespace, str)
                or not re.fullmatch(_LABEL_RE, identity.namespace)
                or not isinstance(identity.name, str) or len(identity.name) > 253
                or not re.fullmatch(_DNS_RE, identity.name)
                or not isinstance(identity.uid, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identity.uid)):
            raise TerminalProviderError("Invalid IRC resource identity.")
        config = self.settings.get((identity.namespace, identity.name))
        if config is None or self.apps is None or self.core is None:
            raise ProviderNotConfigured("IRC provider settings or clients are missing.")
        config.validate()
        return config

    def bind_plan(self, desired: DesiredPlan) -> None:
        """Bind validated desired state before observe; synchronous and IO-free."""
        self._manifests(desired)
        self._plans[desired.identity] = deepcopy(desired)

    def _manifests(self, desired: DesiredPlan) -> tuple[dict, dict]:
        config = self._config(desired.identity)
        identity = desired.identity
        try:
            spec = desired.intent["spec"]
            validate_resource_spec("IRCServer", spec)
            expected_intent = {"kind": "IRCServer", "spec": spec,
                               "server": {"id": spec["id"], "endpoint": spec["endpoint"]},
                               "dependencies": []}
            host = f"{config.service_name}.{identity.namespace}.svc.{config.cluster_domain}"
            if (len(host) > 253 or desired.intent != expected_intent
                    or spec["endpoint"] != f"irc://{host}:6667"
                    or desired.revision != _digest(desired.intent)
                    or desired.idempotency_key != _digest(
                        _identity_values(identity) + [desired.revision])):
                raise ValueError()
        except (KeyError, TypeError, ValueError, ResourceSpecError):
            raise TerminalProviderError("Unsupported IRC desired intent or endpoint.") from None
        labels = {_LABEL: _digest(_identity_values(identity))[:32]}
        annotations = {_OWNER: _json(_identity_values(identity)),
                       _REVISION: desired.revision, _KEY: desired.idempotency_key}
        metadata = {"name": config.service_name, "namespace": identity.namespace,
                    "labels": labels, "annotations": annotations}
        container = {
            "name": "inspircd", "image": config.image, "imagePullPolicy": "IfNotPresent",
            # Empty command/args preserve the verified image entrypoint/defaults.
            "command": [], "args": [], "envFrom": [], "volumeMounts": [],
            # No arbitrary lifecycle hooks or competing health checks. Null is
            # a valid absent optional field and clears a previously added one.
            "lifecycle": None, "livenessProbe": None,
            "env": [{"name": "INSP_SERVER_NAME", "value": host},
                    {"name": "INSP_NET_NAME", "value": config.network_name},
                    {"name": "INSP_ENABLE_DNSBL", "value": "no"}],
            "ports": [{"name": "irc", "containerPort": 6667, "protocol": "TCP",
                       "hostPort": 0, "hostIP": ""}],
            "securityContext": {"allowPrivilegeEscalation": False, "privileged": False,
                                "readOnlyRootFilesystem": False,
                                # Container fields override pod-level defaults; manage both.
                                "runAsNonRoot": True, "runAsUser": 10000, "runAsGroup": 10000,
                                "seccompProfile": {"type": "RuntimeDefault"},
                                "capabilities": {"drop": ["ALL"], "add": []}},
            "readinessProbe": {"tcpSocket": {"port": "irc", "host": ""}, "initialDelaySeconds": 5,
                               "periodSeconds": 5, "timeoutSeconds": 2,
                               "failureThreshold": 3, "successThreshold": 1},
            # Certificate and DH generation in the source entrypoint can be slow.
            "startupProbe": {"tcpSocket": {"port": "irc", "host": ""}, "periodSeconds": 5,
                             "timeoutSeconds": 2, "failureThreshold": 120,
                             "successThreshold": 1},
        }
        deployment = {
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": deepcopy(metadata),
            "spec": {"replicas": 1, "paused": False, "strategy": {"type": "Recreate"},
                     "selector": {"matchLabels": labels, "matchExpressions": []},
                     "template": {
                         "metadata": {"labels": labels, "annotations": annotations},
                         "spec": {"automountServiceAccountToken": False,
                                  "enableServiceLinks": False, "hostNetwork": False,
                                  "hostPID": False, "hostIPC": False,
                                  "initContainers": [], "volumes": [],
                                  "securityContext": {"runAsNonRoot": True,
                                                      "runAsUser": 10000, "runAsGroup": 10000,
                                                      "seccompProfile": {"type": "RuntimeDefault"}},
                                  "containers": [container]}}},
        }
        service = {
            "apiVersion": "v1", "kind": "Service", "metadata": deepcopy(metadata),
            "spec": {"type": "ClusterIP", "selector": labels, "externalIPs": [],
                     "publishNotReadyAddresses": False,
                     "ports": [{"name": "irc", "port": 6667, "targetPort": "irc",
                                "protocol": "TCP"}]},
        }
        return deployment, service

    async def _call(self, method: Any, *, write: bool = False, **kwargs: Any) -> Any:
        kwargs.setdefault("_request_timeout", 30)
        try:
            return _wire(await method(**kwargs))
        except ApiException as error:
            if error.status == 404 and not write:
                return None
            # JSON Patch test failures (including resourceVersion races) use
            # HTTP 422 on Kubernetes. Re-observe instead of treating as success.
            if (error.status == 422 and write
                    and kwargs.get("_content_type") == "application/json-patch+json"):
                raise RetryableProviderError("IRC conditional patch requires re-observation.") from None
            if error.status in {400, 401, 403, 405, 422}:
                raise TerminalProviderError("Kubernetes rejected the IRC request.") from None
            if error.status in {404, 409, 429}:
                raise RetryableProviderError("IRC Kubernetes state must be observed again.") from None
            exception = UncertainOutcome if write else RetryableProviderError
            raise exception("IRC Kubernetes request did not complete reliably.") from None
        except (TimeoutError, OSError, ClientError):
            exception = UncertainOutcome if write else RetryableProviderError
            raise exception("IRC Kubernetes request did not complete reliably.") from None

    async def _read_objects(self, identity: ResourceIdentity) -> tuple[Any, Any]:
        config = self._config(identity)
        args = {"name": config.service_name, "namespace": identity.namespace}
        deployment = await self._call(self.apps.read_namespaced_deployment, **args)
        service = await self._call(self.core.read_namespaced_service, **args)
        for actual in (deployment, service):
            if actual is not None:
                self._check_owner(actual, identity, config.service_name)
        return deployment, service

    @staticmethod
    def _check_owner(actual: dict, identity: ResourceIdentity, name: str) -> None:
        meta = actual.get("metadata", {})
        if (meta.get("namespace") != identity.namespace or meta.get("name") != name
                or meta.get("annotations", {}).get(_OWNER) != _json(_identity_values(identity))
                or not meta.get("uid") or not meta.get("resourceVersion")
                or meta.get("ownerReferences") or meta.get("deletionTimestamp")):
            raise TerminalProviderError("IRC workload ownership is conflicting or terminating.")

    @staticmethod
    def _converged(actual: dict, expected: dict) -> bool:
        if not _matches(actual, expected):
            return False
        # Service selectors and Deployment matchLabels are exact, not subsets:
        # an extra selector label can silently eliminate every backend.
        if expected["kind"] == "Service":
            spec = actual.get("spec", {})
            return (spec.get("selector") == expected["spec"]["selector"]
                    and not spec.get("externalName")
                    and spec.get("clusterIP") != "None")
        selector = actual.get("spec", {}).get("selector", {})
        return (selector.get("matchLabels") == expected["spec"]["selector"]["matchLabels"]
                and not actual.get("spec", {}).get("strategy", {}).get("rollingUpdate"))

    async def observe(self, identity: ResourceIdentity) -> Observation:
        self._config(identity)
        desired = self._plans.get(identity)
        if desired is None:
            raise ProviderNotConfigured("Bind the current IRC plan before observation.")
        expected_deployment, expected_service = self._manifests(desired)
        deployment, service = await self._read_objects(identity)
        if deployment is None and service is None:
            return Observation()
        if (deployment is None or service is None
                or not self._converged(deployment, expected_deployment)
                or not self._converged(service, expected_service)):
            return Observation(exists=True)
        ready = self._deployment_ready(deployment) and bool(service["spec"].get("clusterIP"))
        if ready:
            ready = await self._backends_ready(identity, expected_deployment["spec"]["template"])
        return Observation(True, desired.revision, ready)

    @staticmethod
    def _deployment_ready(deployment: dict) -> bool:
        status = deployment.get("status", {})
        generation = deployment.get("metadata", {}).get("generation")
        observed = status.get("observedGeneration")
        return (isinstance(generation, int) and generation > 0
                and isinstance(observed, int) and observed >= generation
                and all(status.get(key) == 1 for key in (
                    "replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
                and status.get("unavailableReplicas", 0) == 0
                and any(condition.get("type") == "Available" and condition.get("status") == "True"
                        for condition in status.get("conditions", [])))

    async def _backends_ready(self, identity: ResourceIdentity, template: dict) -> bool:
        config = self._config(identity)
        endpoints = await self._call(self.core.read_namespaced_endpoints,
                                     name=config.service_name, namespace=identity.namespace)
        if not endpoints or endpoints.get("metadata", {}).get("deletionTimestamp"):
            return False
        found = False
        for subset in endpoints.get("subsets", []):
            if (subset.get("notReadyAddresses") or not subset.get("addresses")
                    or subset.get("ports") != [{"name": "irc", "port": 6667, "protocol": "TCP"}]):
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
                        or meta.get("namespace") != identity.namespace
                        or not _matches(pod, template) or address.get("ip") not in pod_ips
                        or not address.get("ip") or status.get("phase") != "Running"
                        or not any(c.get("type") == "Ready" and c.get("status") == "True"
                                   for c in status.get("conditions", []))):
                    return False
                found = True
        return found

    async def ensure(self, desired: DesiredPlan, *, idempotency_key: str) -> None:
        if idempotency_key != desired.idempotency_key:
            raise TerminalProviderError("IRC idempotency key does not match desired intent.")
        expected = self._manifests(desired)
        self.bind_plan(desired)
        actual = await self._read_objects(desired.identity)
        # Preflight every collision/immutable mismatch before any partial update.
        if actual[0] is not None:
            selector = actual[0].get("spec", {}).get("selector", {})
            if (selector.get("matchLabels") != expected[0]["spec"]["selector"]["matchLabels"]
                    or selector.get("matchExpressions")):
                raise TerminalProviderError("IRC Deployment selector is immutable and conflicting.")
        if actual[1] is not None and actual[1].get("spec", {}).get("clusterIP") == "None":
            raise TerminalProviderError("IRC Service requires an allocated ClusterIP.")
        for old, body, create, patch in zip(actual, expected,
                (self.apps.create_namespaced_deployment, self.core.create_namespaced_service),
                (self.apps.patch_namespaced_deployment, self.core.patch_namespaced_service)):
            if old is None:
                await self._call(create, write=True, namespace=desired.identity.namespace, body=body)
            elif not self._converged(old, body):
                changes = self._patch(old, body)
                await self._call(patch, write=True, namespace=desired.identity.namespace,
                                 name=body["metadata"]["name"], body=changes,
                                 _content_type="application/json-patch+json")

    @staticmethod
    def _patch(old: dict, desired: dict) -> list[dict]:
        """Conditional patch of owned fields, preserving unrelated metadata/defaults."""
        changes = [
            {"op": "test", "path": "/metadata/uid", "value": old["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion",
             "value": old["metadata"]["resourceVersion"]},
        ]

        def visit(actual: Any, expected: Any, path: str) -> None:
            if _matches(actual, expected):
                return
            if isinstance(expected, dict) and isinstance(actual, dict):
                for key, value in expected.items():
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    visit(actual.get(key), value, f"{path}/{escaped}")
            else:
                changes.append({"op": "add", "path": path, "value": deepcopy(expected)})

        visit(old, desired, "")
        if desired["kind"] == "Service":
            # Remove extra selector keys; replacing this object does not touch
            # server-allocated ClusterIP/clusterIPs/IP-family fields.
            if old.get("spec", {}).get("selector") != desired["spec"]["selector"]:
                changes.append({"op": "add", "path": "/spec/selector",
                                "value": deepcopy(desired["spec"]["selector"])})
            if "externalName" in old.get("spec", {}):
                changes.append({"op": "remove", "path": "/spec/externalName"})
        elif "rollingUpdate" in old.get("spec", {}).get("strategy", {}):
            changes.append({"op": "remove", "path": "/spec/strategy/rollingUpdate"})
        return changes
