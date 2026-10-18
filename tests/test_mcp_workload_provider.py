"""Offline payload, retention, drift, CAS and readiness-gate contracts."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from kubernetes_asyncio.client import ApiClient
from kubernetes_asyncio.client.exceptions import ApiException
import pytest

from cuse_operator.controllers.base import PlanContext, make_plan
from cuse_operator.controllers.mcpclient import build_plan as mcp_plan
from cuse_operator.controllers.weechat import build_plan as weechat_plan
from cuse_operator.irc_provider import _wire
from cuse_operator.mcp_config import deny_all_policies, render_mcp_config
from cuse_operator.mcp_workload_provider import (
    MCPClientKubernetesProvider, MCPClientSettings, SecretKeyRef,
    WeeChatKubernetesProvider, WeeChatSettings,
)
from cuse_operator.providers import (
    ProviderNotConfigured, RetryableProviderError, TerminalProviderError, UncertainOutcome,
)


def run(awaitable):
    return asyncio.run(awaitable)


def body(kind, name, spec, uid=None):
    return {"apiVersion": "cuse.io/v1alpha1", "kind": kind,
            "metadata": {"namespace": "test", "name": name, "uid": uid or name + "-uid"},
            "spec": spec}


def fixture_plan(kind="WeeChat", uid=None):
    server = body("IRCServer", "server", {"id": "irc", "endpoint": "irc://irc.test:6667"})
    chat = body("WeeChat", "chat", {"id": "chat", "serverRef": {"name": "server"},
                "nick": "research-bot", "mcp_endpoint": "http://runtime.test.svc.cluster.local:8000/mcp"},
                uid=uid if kind == "WeeChat" else None)
    connector = body("Connector", "connector", {"id": "chat", "serverRef": {"name": "server"},
                     "weechatRef": {"name": "chat"}, "nick": "research-bot",
                     "mcp_endpoint": chat["spec"]["mcp_endpoint"]})
    client = body("MCPClient", "client", {"id": "client",
                  "endpoint": "http://runtime.test.svc.cluster.local:8000/mcp",
                  "connectorRefs": [{"name": "connector"}]}, uid=uid)
    context = PlanContext("test", [server, chat, connector, client])
    return weechat_plan(chat, context) if kind == "WeeChat" else mcp_plan(client, context)


def config(kind):
    common = dict(image="registry.example.test/runtime@sha256:" + "a" * 64,
                  workload_name="runtime", cluster_domain="cluster.local", http_port=8000,
                  bind_host="0.0.0.0", allowed_hosts=("runtime.test.svc.cluster.local:8000",),
                  storage_mode="ephemeral")
    if kind == "WeeChat":
        return WeeChatSettings(**common, channels=("#research",), full_control=False,
                               auth_secret=SecretKeyRef("weechat-auth", "token"))
    return MCPClientSettings(**common, endpoint_mode="internal-unauthenticated",
                             policies=deny_all_policies(),
                             secret_profiles={"chat": SecretKeyRef("weechat-auth", "token")},
                             supports_bearer_file=True)


class FakeKubernetes:
    """Stores API-shaped resources, defaults and exact conditional JSON patches."""
    def __init__(self):
        self.objects, self.calls, self.fail, self.after_commit = {}, [], {}, {}
        self.before_patch = None

    def object(self, kind):
        return self.objects[kind, "test", "runtime"]

    def _record(self, verb, kind, kwargs):
        self.calls.append((verb, kind, deepcopy(kwargs)))
        if (verb, kind) in self.fail:
            raise self.fail.pop((verb, kind))

    async def read(self, kind, namespace, name):
        self._record("read", kind, dict(namespace=namespace, name=name))
        if (kind, namespace, name) not in self.objects:
            raise ApiException(status=404)
        return deepcopy(self.objects[kind, namespace, name])

    async def create(self, kind, namespace, body):
        self._record("create", kind, dict(namespace=namespace, body=body))
        key = kind, namespace, body["metadata"]["name"]
        if key in self.objects:
            raise ApiException(status=409)
        stored = _wire(deepcopy(body))
        stored["metadata"].update(uid="kube-" + kind, resourceVersion="1", generation=1)
        if kind == "Service":
            stored["spec"].update(clusterIP="10.96.0.1", clusterIPs=["10.96.0.1"],
                                  ipFamilies=["IPv4"], ipFamilyPolicy="SingleStack")
        self.objects[key] = stored
        if ("create", kind) in self.after_commit:
            raise self.after_commit.pop(("create", kind))
        return deepcopy(stored)

    async def patch(self, kind, namespace, name, body, _content_type):
        assert _content_type == "application/json-patch+json"
        self._record("patch", kind, dict(namespace=namespace, name=name, body=body))
        key = kind, namespace, name
        if self.before_patch:
            self.before_patch(self.objects[key])
            self.before_patch = None
        stored = deepcopy(self.objects[key])
        original_spec = deepcopy(stored.get("spec"))
        for change in body:
            keys = [part.replace("~1", "/").replace("~0", "~")
                    for part in change["path"].strip("/").split("/")]
            target = stored
            for part in keys[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            field = int(keys[-1]) if isinstance(target, list) else keys[-1]
            if change["op"] == "test":
                if target[field] != change["value"]:
                    raise ApiException(status=422)
            elif change["op"] == "add":
                target[field] = deepcopy(change["value"])
            elif change["op"] == "remove":
                del target[field]
            else:
                raise AssertionError(change)
        stored["metadata"]["resourceVersion"] = str(int(stored["metadata"]["resourceVersion"]) + 1)
        if stored.get("spec") != original_spec:
            stored["metadata"]["generation"] += 1
        self.objects[key] = _wire(stored)
        if ("patch", kind) in self.after_commit:
            raise self.after_commit.pop(("patch", kind))
        return deepcopy(stored)

    def __getattr__(self, name):
        kinds = {"config_map": "ConfigMap", "deployment": "Deployment", "service": "Service",
                 "endpoints": "Endpoints", "pod": "Pod"}
        for verb in ("read", "create", "patch"):
            prefix = verb + "_namespaced_"
            if name.startswith(prefix) and name[len(prefix):] in kinds:
                async def call(**kwargs):
                    assert kwargs.pop("_request_timeout") == 30
                    return await getattr(self, verb)(kinds[name[len(prefix):]], **kwargs)
                return call
        # A Secret read, delete or any invented API is a test failure.
        raise AttributeError(name)

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "read"]

    def ready(self):
        deployment = self.object("Deployment")
        deployment["status"] = dict(observedGeneration=deployment["metadata"]["generation"],
            replicas=1, updatedReplicas=1, readyReplicas=1, availableReplicas=1,
            unavailableReplicas=0, conditions=[{"type": "Available", "status": "True"}])
        pod = deepcopy(deployment["spec"]["template"])
        pod.update(apiVersion="v1", kind="Pod")
        pod["metadata"].update(namespace="test", name="runtime-pod", uid="pod-uid")
        pod["status"] = dict(phase="Running", podIP="10.0.1.5", podIPs=[{"ip": "10.0.1.5"}],
                             conditions=[{"type": "Ready", "status": "True"}])
        self.objects["Pod", "test", "runtime-pod"] = pod
        self.objects["Endpoints", "test", "runtime"] = {
            "metadata": {"name": "runtime", "namespace": "test"},
            "subsets": [{"addresses": [{"ip": "10.0.1.5", "targetRef": {
                "kind": "Pod", "namespace": "test", "name": "runtime-pod", "uid": "pod-uid"}}],
                "ports": [{"name": "mcp", "port": 8000, "protocol": "TCP"}]}]}


class Gate:
    def __init__(self, result=True):
        self.result, self.calls = result, []

    async def observe(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(ready=self.result)


def setup(kind="WeeChat", readiness=None, settings=None):
    desired, kube = fixture_plan(kind), FakeKubernetes()
    cls = WeeChatKubernetesProvider if kind == "WeeChat" else MCPClientKubernetesProvider
    provider = cls(apps=kube, core=kube,
                   settings={("test", desired.identity.name): settings or config(kind)},
                   readiness=readiness)
    provider.bind_plan(desired)
    return kube, provider, desired


def ensure(provider, desired):
    run(provider.ensure(desired, idempotency_key=desired.idempotency_key))


@pytest.fixture(params=["WeeChat", "MCPClient"])
def configured(request):
    return setup(request.param)


def test_create_retained_objects_noop_and_fail_closed_readiness(configured):
    kube, provider, desired = configured
    assert not run(provider.observe(desired.identity)).exists
    ensure(provider, desired)
    observed = run(provider.observe(desired.identity))
    assert observed.exists and observed.revision == desired.revision and not observed.ready
    assert [(verb, kind) for verb, kind, _ in kube.writes] == [
        ("create", "ConfigMap"), ("create", "Deployment"), ("create", "Service")]
    kube.ready()
    assert not run(provider.observe(desired.identity)).ready
    ensure(provider, desired)
    assert len(kube.writes) == 3
    assert "ownerReferences" not in str(kube.objects) and "finalizers" not in str(kube.objects)
    assert not hasattr(provider, "delete")
    assert kube.object("Deployment")["spec"]["replicas"] == 1
    assert kube.object("Deployment")["spec"]["strategy"] == {"type": "Recreate"}


def test_weechat_source_contract_and_secret_not_rendered():
    kube, provider, desired = setup()
    ensure(provider, desired)
    data = kube.object("ConfigMap")["data"]
    assert data["IRC_HOST"] == "irc.test" and data["IRC_PORT"] == "6667"
    assert data["IRC_TLS"] == "false" and data["IRC_NICK"] == "research-bot"
    assert data["IRC_CHANNELS"] == "#research" and "#wchat" not in str(kube.objects)
    assert data["MCP_TRANSPORT"] == "http" and data["MCP_AUTH_MODE"] == "bearer"
    assert data["MCP_HOST"] == "0.0.0.0" and data["PORT"] == "8000"
    assert data["WEECHAT_HOME"] == "" and data["WEECHAT_MCP_FULL_CONTROL"] == "0"
    assert data["MCP_EVENTS_DB"] == ""
    pod = kube.object("Deployment")["spec"]["template"]["spec"]
    assert len(pod["containers"]) == 1 and pod["initContainers"] == []
    container = pod["containers"][0]
    assert container["command"] == container["args"] == container["envFrom"] == []
    auth = next(item for item in container["env"] if item["name"] == "MCP_AUTH_TOKEN")
    assert auth == {"name": "MCP_AUTH_TOKEN", "valueFrom": {"secretKeyRef": {
        "name": "weechat-auth", "key": "token", "optional": False}}}
    assert "MCP_AUTH_TOKEN" not in data and pod["securityContext"]["runAsUser"] == 10001
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {}}]


def test_mcp_renderer_artifacts_args_secret_projections_and_uid():
    kube, provider, desired = setup("MCPClient")
    ensure(provider, desired)
    cfg = config("MCPClient")
    rendered = render_mcp_config(desired, bind_host=cfg.bind_host, http_port=8000,
        allowed_hosts=cfg.allowed_hosts, policies=cfg.policies,
        bearer_token_files={"chat": "/var/run/secrets/cuse-mcp/downstream-0/token"})
    data = kube.object("ConfigMap")["data"]
    assert data["server.json"] == rendered.config_json
    assert data["mcp-tools-0.rego"] == rendered.policy_files[0][1]
    pod = kube.object("Deployment")["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["args"] == list(rendered.args)
    assert container["securityContext"]["runAsUser"] == pod["securityContext"]["fsGroup"] == 1000
    assert container["env"][0] == {"name": "MCP_V8_ALLOWED_HOSTS",
                                    "value": "runtime.test.svc.cluster.local:8000"}
    secret = next(volume for volume in pod["volumes"] if "secret" in volume)
    assert secret["secret"] == {"secretName": "weechat-auth", "optional": False,
        "defaultMode": 0o440, "items": [{"key": "token", "path": "token"}]}
    mount = next(m for m in container["volumeMounts"] if m["name"] == secret["name"])
    assert mount["readOnly"] and "subPath" not in mount


@pytest.mark.parametrize("kind", ["WeeChat", "MCPClient"])
def test_protocol_gate_only_after_current_backend_and_exact_arguments(kind):
    gate = Gate()
    kube, provider, desired = setup(kind, readiness=gate)
    ensure(provider, desired)
    assert not run(provider.observe(desired.identity)).ready and not gate.calls
    kube.ready()
    assert run(provider.observe(desired.identity)).ready
    assert gate.calls[-1] == dict(endpoint="http://runtime.test.svc.cluster.local:8000/mcp",
        provider=kind, expected_server="irc://irc.test:6667" if kind == "WeeChat" else None,
        expected_nick="research-bot" if kind == "WeeChat" else None)
    gate.result = 1  # Only a literal boolean attestation counts.
    assert not run(provider.observe(desired.identity)).ready
    gate.result = True
    kube.objects["Pod", "test", "runtime-pod"]["metadata"]["uid"] = "other-pod"
    count = len(gate.calls)
    assert not run(provider.observe(desired.identity)).ready and len(gate.calls) == count


@pytest.mark.parametrize("path,value", [
    (("securityContext", "runAsUser"), 0),
    (("securityContext", "allowPrivilegeEscalation"), True),
    (("securityContext", "readOnlyRootFilesystem"), False),
    (("securityContext", "privileged"), True),
    (("securityContext", "capabilities", "add"), ["SYS_ADMIN"]),
    (("securityContext", "procMount"), "Unmasked"),
    (("securityContext", "seLinuxOptions"), {"type": "spc_t"}),
    (("securityContext", "appArmorProfile"), {"type": "Unconfined"}),
    (("ports", 0, "hostPort"), 8000),
    (("ports", 0, "hostIP"), "0.0.0.0"),
    (("command",), ["sh", "-c", "echo bad"]),
    (("args",), ["--allowed-hosts", "*"]),
    (("envFrom",), [{"secretRef": {"name": "other"}}]),
    (("lifecycle",), {"postStart": {"exec": {"command": ["sh"]}}}),
    (("livenessProbe",), {"exec": {"command": ["sh"]}}),
    (("readinessProbe", "tcpSocket", "host"), "elsewhere"),
])
def test_execution_security_probe_and_port_drift_is_corrected(configured, path, value):
    kube, provider, desired = configured
    ensure(provider, desired)
    container = kube.object("Deployment")["spec"]["template"]["spec"]["containers"][0]
    target = container
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert run(provider.observe(desired.identity)).revision == ""
    ensure(provider, desired)
    assert run(provider.observe(desired.identity)).revision == desired.revision


def test_kubernetes_false_and_empty_defaults_do_not_create_perpetual_drift(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    deployment, service = kube.object("Deployment"), kube.object("Service")
    deployment["spec"].pop("paused")
    pod = deployment["spec"]["template"]["spec"]
    for field in ("hostNetwork", "hostPID", "hostIPC", "initContainers"):
        pod.pop(field)
    container = pod["containers"][0]
    container["securityContext"].pop("privileged")
    for field in ("hostPort", "hostIP"):
        container["ports"][0].pop(field)
    container["readinessProbe"]["tcpSocket"].pop("host")
    service["spec"].pop("publishNotReadyAddresses")
    service["spec"].pop("externalIPs")
    kube.object("ConfigMap").pop("binaryData")
    assert run(provider.observe(desired.identity)).revision == desired.revision
    ensure(provider, desired)
    assert len(kube.writes) == 3
    # A missing pointer-valued security boolean is NOT equivalent to false.
    container["securityContext"].pop("allowPrivilegeEscalation")
    assert run(provider.observe(desired.identity)).revision == ""


def test_configmap_exact_data_metadata_and_service_allocations_survive_patch(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    cm, service = kube.object("ConfigMap"), kube.object("Service")
    cm["metadata"]["annotations"]["unrelated"] = "keep"
    cm["data"]["MCP_AUTH_TOKEN"] = "an-unrequested-injection"
    cm["binaryData"] = {"foreign": "eA=="}
    service["spec"]["selector"]["unrelated"] = "bad-selector"
    service["spec"]["externalName"] = "other.example"
    assert run(provider.observe(desired.identity)).revision == ""
    ensure(provider, desired)
    assert "MCP_AUTH_TOKEN" not in kube.object("ConfigMap")["data"]
    assert not kube.object("ConfigMap")["binaryData"]
    assert kube.object("ConfigMap")["metadata"]["annotations"]["unrelated"] == "keep"
    assert kube.object("Service")["spec"]["clusterIP"] == "10.96.0.1"
    assert kube.object("Service")["spec"]["ipFamilies"] == ["IPv4"]
    assert "externalName" not in kube.object("Service")["spec"]
    assert run(provider.observe(desired.identity)).revision == desired.revision


@pytest.mark.parametrize("collision_kind", ["ConfigMap", "Deployment", "Service"])
def test_foreign_uid_collision_preflights_before_any_write(configured, collision_kind):
    kube, provider, desired = configured
    ensure(provider, desired)
    original = deepcopy(kube.object(collision_kind))
    kube.objects = {(collision_kind, "test", "runtime"): original}
    kube.calls.clear()
    other = fixture_plan(desired.identity.kind, uid="new-resource-uid")
    with pytest.raises(TerminalProviderError):
        ensure(provider, other)
    assert not kube.writes


@pytest.mark.parametrize("object_kind,mutate", [
    ("ConfigMap", lambda obj: obj.update(immutable=True)),
    ("Deployment", lambda obj: obj["spec"]["selector"]["matchLabels"].update(extra="x")),
    ("Service", lambda obj: obj["spec"].update(clusterIP="None")),
    ("Service", lambda obj: obj["metadata"].update(ownerReferences=[{"uid": "someone"}])),
    ("Deployment", lambda obj: obj["metadata"].update(deletionTimestamp="2026-01-01T00:00:00Z")),
])
def test_immutable_or_deleting_conflicts_do_not_partially_update(configured, object_kind, mutate):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.object("ConfigMap")["data"]["extra"] = "drift"
    mutate(kube.object(object_kind))
    count = len(kube.writes)
    with pytest.raises(TerminalProviderError):
        ensure(provider, desired)
    assert len(kube.writes) == count


@pytest.mark.parametrize("field,value", [("resourceVersion", "999"), ("uid", "replacement")])
def test_uid_resource_version_conditional_patches_detect_races(configured, field, value):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.object("ConfigMap")["data"]["extra"] = "drift"
    kube.before_patch = lambda obj: obj["metadata"].update({field: value})
    with pytest.raises(RetryableProviderError):
        ensure(provider, desired)
    assert kube.object("ConfigMap")["data"]["extra"] == "drift"
    patch = kube.writes[-1][2]["body"]
    assert [(p["op"], p["path"]) for p in patch[:2]] == [
        ("test", "/metadata/uid"), ("test", "/metadata/resourceVersion")]


def test_lost_create_response_reobserves_without_duplicate_workload(configured):
    kube, provider, desired = configured
    kube.after_commit["create", "ConfigMap"] = TimeoutError("private details")
    with pytest.raises(UncertainOutcome, match="reliably"):
        ensure(provider, desired)
    assert run(provider.observe(desired.identity)).exists
    ensure(provider, desired)
    assert [(verb, kind) for verb, kind, _ in kube.writes] == [
        ("create", "ConfigMap"), ("create", "Deployment"), ("create", "Service")]


def test_restart_rebind_is_noop_and_only_missing_object_is_recreated(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    restarted = type(provider)(apps=kube, core=kube, settings=provider.settings)
    with pytest.raises(ProviderNotConfigured):
        run(restarted.observe(desired.identity))
    restarted.bind_plan(desired)
    ensure(restarted, desired)
    assert len(kube.writes) == 3
    del kube.objects["Service", "test", "runtime"]
    assert run(restarted.observe(desired.identity)).revision == ""
    ensure(restarted, desired)
    assert [(verb, kind) for verb, kind, _ in kube.writes[3:]] == [("create", "Service")]


@pytest.mark.parametrize("verb,status,error_type", [
    ("read", 403, TerminalProviderError), ("create", 400, TerminalProviderError),
    ("create", 403, TerminalProviderError), ("create", 409, RetryableProviderError),
    ("create", 422, TerminalProviderError), ("create", 429, RetryableProviderError),
    ("create", 500, UncertainOutcome), ("read", 503, RetryableProviderError),
])
def test_api_failure_categories_never_include_server_error_content(verb, status, error_type):
    kube, provider, desired = setup()
    kube.fail[verb, "ConfigMap"] = ApiException(status=status, reason="confidential-server-detail")
    with pytest.raises(error_type) as caught:
        ensure(provider, desired)
    assert "confidential" not in str(caught.value)


def test_lost_patch_response_reobserves_without_duplicate_mutation(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.object("ConfigMap")["data"]["extra"] = "drift"
    kube.after_commit["patch", "ConfigMap"] = TimeoutError()
    with pytest.raises(UncertainOutcome):
        ensure(provider, desired)
    assert run(provider.observe(desired.identity)).revision == desired.revision
    count = len(kube.writes)
    ensure(provider, desired)
    assert len(kube.writes) == count


def test_settings_change_rolls_workload_without_changing_cr_revision(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    old_digest = kube.object("Deployment")["spec"]["template"]["metadata"]["annotations"]["cuse.io/runtime-digest"]
    key = ("test", desired.identity.name)
    provider.settings[key] = replace(provider.settings[key], image="registry.example.test/runtime@sha256:" + "b" * 64)
    assert run(provider.observe(desired.identity)).revision == ""
    ensure(provider, desired)
    new_digest = kube.object("Deployment")["spec"]["template"]["metadata"]["annotations"]["cuse.io/runtime-digest"]
    assert old_digest != new_digest
    assert run(provider.observe(desired.identity)).revision == desired.revision


@pytest.mark.parametrize("kind", ["WeeChat", "MCPClient"])
@pytest.mark.parametrize("changes", [
    {"image": "registry.example.test/runtime:latest"}, {"storage_mode": "persistent"},
    {"allowed_hosts": ("*",)}, {"allowed_hosts": ("other.example",)},
    {"bind_host": "127.0.0.1"}, {"http_port": True}, {"workload_name": "../escape"},
])
def test_explicit_image_network_storage_constraints(kind, changes):
    with pytest.raises(TerminalProviderError):
        setup(kind, settings=replace(config(kind), **changes))


@pytest.mark.parametrize("changes", [
    {"channels": ()}, {"channels": ("#research", "#research")},
    {"channels": ("#research,other",)}, {"full_control": None},
    {"auth_secret": SecretKeyRef("../secret", "token")},
])
def test_weechat_no_implicit_channels_controls_or_secret_location(changes):
    with pytest.raises(TerminalProviderError):
        setup(settings=replace(config("WeeChat"), **changes))


@pytest.mark.parametrize("changes", [
    {"supports_bearer_file": False}, {"policies": {}},
    {"endpoint_mode": "public-unauthenticated"},
    {"secret_profiles": {"not-configured": SecretKeyRef("auth", "token")}},
    {"secret_profiles": {"chat": {"token": "inline-secret"}}},
])
def test_mcp_explicit_policy_and_bearer_image_capability(changes):
    with pytest.raises(TerminalProviderError):
        setup("MCPClient", settings=replace(config("MCPClient"), **changes))


def test_connector_is_not_a_workload_and_unbound_plan_is_not_ready():
    kube = FakeKubernetes()
    provider = WeeChatKubernetesProvider(apps=kube, core=kube, settings={("test", "chat"): config("WeeChat")})
    desired = fixture_plan()
    with pytest.raises(ProviderNotConfigured):
        run(provider.observe(desired.identity))
    with pytest.raises(TerminalProviderError):
        provider.bind_plan(replace(desired, identity=replace(desired.identity, kind="Connector")))
    assert not kube.calls


def test_modified_plan_hash_and_extra_secret_fields_rejected(configured):
    kube, provider, desired = configured
    with pytest.raises(TerminalProviderError):
        ensure(provider, replace(desired, revision="0" * 64))
    with pytest.raises(TerminalProviderError):
        run(provider.ensure(desired, idempotency_key="wrong"))
    intent = deepcopy(desired.intent)
    intent["spec"]["token"] = "inline-not-supported"
    changed = make_plan(body(desired.identity.kind, desired.identity.name, intent["spec"]), intent)
    with pytest.raises(TerminalProviderError):
        ensure(provider, changed)
    assert not kube.calls


def test_real_kubernetes_model_roundtrip(configured):
    kube, provider, desired = configured
    expected = provider._manifests(desired)
    # Kubernetes model field mappings must not turn camelCase into snake_case.
    async def check():
        async with ApiClient() as api:
            for payload, type_name in zip(expected, ("V1ConfigMap", "V1Deployment", "V1Service")):
                model = api._ApiClient__deserialize(payload, type_name)
                assert provider._converged(_wire(model), payload)
    run(check())
