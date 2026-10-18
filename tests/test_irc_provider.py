"""Offline contract tests: fake APIs apply real JSON Patch-shaped requests."""
import asyncio
from copy import deepcopy
from dataclasses import replace

from kubernetes_asyncio.client import V1Deployment, V1ObjectMeta
from kubernetes_asyncio.client.exceptions import ApiException
import pytest

from cuse_operator.controllers.base import make_plan
from cuse_operator.controllers.ircserver import build_plan
from cuse_operator.irc_provider import IRCServerKubernetesProvider, IRCServerSettings, _wire
from cuse_operator.providers import (
    ProviderNotConfigured, RetryableProviderError, TerminalProviderError,
    UncertainOutcome,
)


def run(awaitable):
    return asyncio.run(awaitable)


CONFIG = IRCServerSettings(
    image="registry.example.test/fork/inspircd@sha256:" + "a" * 64,
    service_name="irc-test", cluster_domain="cluster.local", network_name="TestNet",
    endpoint_mode="internal-plaintext",
)


def plan(*, uid="cr-uid-1", server_id="test-network", endpoint=None):
    body = {"apiVersion": "cuse.io/v1alpha1", "kind": "IRCServer",
            "metadata": {"namespace": "test", "name": "server", "uid": uid},
            "spec": {"id": server_id,
                     "endpoint": endpoint or "irc://irc-test.test.svc.cluster.local:6667"}}
    return build_plan(body, None)


class FakeKubernetes:
    """No network/credentials. Defaults mimic real API metadata and rollout lag."""
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail = {}
        self.after_commit = {}
        self.before_patch = None

    @staticmethod
    def key(kind, namespace, name):
        return kind, namespace, name

    def object(self, kind):
        return self.objects[self.key(kind, "test", "irc-test")]

    def _record(self, verb, kind, **kwargs):
        self.calls.append((verb, kind, deepcopy(kwargs)))
        error = self.fail.pop((verb, kind), None)
        if error:
            raise error

    async def read(self, kind, *, namespace, name):
        self._record("read", kind, namespace=namespace, name=name)
        body = self.objects.get(self.key(kind, namespace, name))
        if body is None:
            raise ApiException(status=404)
        return deepcopy(body)

    async def create(self, kind, *, namespace, body):
        self._record("create", kind, namespace=namespace, body=body)
        key = self.key(kind, namespace, body["metadata"]["name"])
        if key in self.objects:
            raise ApiException(status=409)
        stored = deepcopy(body)
        stored["metadata"].update(uid=f"kube-{kind}-uid", resourceVersion="1", generation=1)
        if kind == "Service":
            stored["spec"].update(clusterIP="10.96.0.25", clusterIPs=["10.96.0.25"],
                                  ipFamilies=["IPv4"], ipFamilyPolicy="SingleStack")
        self.objects[key] = stored
        error = self.after_commit.pop(("create", kind), None)
        if error:
            raise error
        return deepcopy(stored)

    async def patch(self, kind, *, namespace, name, body, _content_type):
        assert _content_type == "application/json-patch+json"
        self._record("patch", kind, namespace=namespace, name=name, body=body)
        key = self.key(kind, namespace, name)
        if self.before_patch:
            self.before_patch(self.objects[key])
            self.before_patch = None
        stored = deepcopy(self.objects[key])
        before_spec = deepcopy(stored["spec"])
        for change in body:
            keys = [part.replace("~1", "/").replace("~0", "~")
                    for part in change["path"].strip("/").split("/")]
            target = stored
            for part in keys[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            key_part = int(keys[-1]) if isinstance(target, list) else keys[-1]
            if change["op"] == "test":
                if target.get(key_part) != change["value"]:
                    raise ApiException(status=422)
            elif change["op"] == "add":
                target[key_part] = deepcopy(change["value"])
            elif change["op"] == "remove":
                del target[key_part]
            else:
                raise AssertionError(change)
        stored["metadata"]["resourceVersion"] = str(int(stored["metadata"]["resourceVersion"]) + 1)
        if stored["spec"] != before_spec:
            stored["metadata"]["generation"] += 1
        self.objects[key] = stored
        error = self.after_commit.pop(("patch", kind), None)
        if error:
            raise error
        return deepcopy(stored)

    def __getattr__(self, name):
        verbs = {"read_namespaced_": self.read, "create_namespaced_": self.create,
                 "patch_namespaced_": self.patch}
        kinds = {"deployment": "Deployment", "service": "Service", "endpoints": "Endpoints",
                 "pod": "Pod"}
        for prefix, operation in verbs.items():
            if name.startswith(prefix) and name[len(prefix):] in kinds:
                async def call(**kwargs):
                    assert kwargs.pop("_request_timeout") == 30
                    return await operation(kinds[name[len(prefix):]], **kwargs)
                return call
        raise AttributeError(name)

    def ready(self):
        deployment = self.object("Deployment")
        deployment["status"] = {
            "observedGeneration": deployment["metadata"]["generation"],
            "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1,
            "unavailableReplicas": 0, "conditions": [{"type": "Available", "status": "True"}],
        }
        pod = deepcopy(deployment["spec"]["template"])
        pod.update(apiVersion="v1", kind="Pod")
        pod["metadata"].update(name="irc-pod", namespace="test", uid="pod-uid")
        pod["status"] = {"phase": "Running", "podIP": "10.0.1.5", "podIPs": [{"ip": "10.0.1.5"}],
                         "conditions": [{"type": "Ready", "status": "True"}]}
        self.objects[self.key("Pod", "test", "irc-pod")] = pod
        self.objects[self.key("Endpoints", "test", "irc-test")] = {
            "apiVersion": "v1", "kind": "Endpoints",
            "metadata": {"name": "irc-test", "namespace": "test", "resourceVersion": "3"},
            "subsets": [{"addresses": [{"ip": "10.0.1.5", "targetRef": {
                "kind": "Pod", "namespace": "test", "name": "irc-pod", "uid": "pod-uid"}}],
                "ports": [{"name": "irc", "port": 6667, "protocol": "TCP"}]}],
        }

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "read"]


@pytest.fixture
def configured():
    kube = FakeKubernetes()
    provider = IRCServerKubernetesProvider(apps=kube, core=kube,
                                         settings={("test", "server"): CONFIG})
    desired = plan()
    provider.bind_plan(desired)
    return kube, provider, desired


def ensure(provider, desired):
    run(provider.ensure(desired, idempotency_key=desired.idempotency_key))


def test_create_wait_for_real_readiness_and_noop(configured):
    kube, provider, desired = configured
    assert not run(provider.observe(desired.identity)).exists
    ensure(provider, desired)
    observed = run(provider.observe(desired.identity))
    assert observed.exists and observed.revision == desired.revision and not observed.ready
    assert [(verb, kind) for verb, kind, _ in kube.writes] == [
        ("create", "Deployment"), ("create", "Service")]
    kube.ready()
    assert run(provider.observe(desired.identity)).ready
    ensure(provider, desired)
    assert len(kube.writes) == 2
    service = kube.object("Service")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [{"name": "irc", "port": 6667,
                                         "targetPort": "irc", "protocol": "TCP"}]
    deployment = kube.object("Deployment")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == CONFIG.image
    assert container["command"] == container["args"] == []
    assert container["env"] == [
        {"name": "INSP_SERVER_NAME", "value": "irc-test.test.svc.cluster.local"},
        {"name": "INSP_NET_NAME", "value": "TestNet"},
        {"name": "INSP_ENABLE_DNSBL", "value": "no"}]
    assert "ownerReferences" not in str(kube.objects)
    assert "finalizers" not in str(kube.objects)
    assert not hasattr(provider, "delete")


def test_update_patch_preserves_server_allocations_and_unmanaged_metadata(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    service = kube.object("Service")
    service["metadata"]["annotations"]["example.org/note"] = "keep"
    service["metadata"]["labels"]["team"] = "keep"
    newer = plan(server_id="new-domain-id")
    provider.bind_plan(newer)
    assert run(provider.observe(newer.identity)).revision != newer.revision
    ensure(provider, newer)
    service = kube.object("Service")
    assert service["spec"]["clusterIP"] == "10.96.0.25"
    assert service["spec"]["clusterIPs"] == ["10.96.0.25"]
    assert service["metadata"]["annotations"]["example.org/note"] == "keep"
    assert service["metadata"]["labels"]["team"] == "keep"
    assert not run(provider.observe(newer.identity)).ready
    kube.ready()
    assert run(provider.observe(newer.identity)).ready
    for verb, _, args in kube.writes:
        if verb == "patch":
            assert [p["path"] for p in args["body"][:2]] == [
                "/metadata/uid", "/metadata/resourceVersion"]
            assert all("clusterIP" not in p["path"] for p in args["body"])


@pytest.mark.parametrize("field", ["image", "env", "command", "readinessProbe", "volumes",
                                   "selector", "ports", "externalIPs", "revision"])
def test_real_drift_is_detected_and_repaired_even_with_matching_revision(configured, field):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    pod_spec = kube.object("Deployment")["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    service_spec = kube.object("Service")["spec"]
    if field == "image":
        container["image"] = "wrong/image:tag"
    elif field == "env":
        container["env"][1]["value"] = "OtherNetwork"
    elif field == "command":
        container["command"] = ["sleep", "infinity"]
    elif field == "readinessProbe":
        container["readinessProbe"]["tcpSocket"]["port"] = 9999
    elif field == "volumes":
        pod_spec["volumes"] = [{"name": "unexpected", "emptyDir": {}}]
    elif field == "selector":
        service_spec["selector"]["extra"] = "wrong"
    elif field == "ports":
        service_spec["ports"][0]["targetPort"] = 9999
    elif field == "externalIPs":
        service_spec["externalIPs"] = ["198.51.100.1"]
    elif field == "revision":
        kube.object("Service")["metadata"]["annotations"]["cuse.io/irc-revision"] = "b" * 64
    observed = run(provider.observe(desired.identity))
    assert observed.exists and not observed.ready and observed.revision != desired.revision
    ensure(provider, desired)
    kube.ready()
    assert run(provider.observe(desired.identity)).ready
    written = len(kube.writes)
    ensure(provider, desired)
    assert len(kube.writes) == written


def test_restart_requires_fresh_binding_and_checks_config_changes(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    restarted = IRCServerKubernetesProvider(apps=kube, core=kube,
        settings={("test", "server"): replace(CONFIG, image=CONFIG.image[:-64] + "b" * 64)})
    with pytest.raises(ProviderNotConfigured):
        run(restarted.observe(desired.identity))
    restarted.bind_plan(desired)
    assert not run(restarted.observe(desired.identity)).ready
    ensure(restarted, desired)
    kube.ready()
    assert run(restarted.observe(desired.identity)).ready
    assert kube.object("Deployment")["spec"]["template"]["spec"]["containers"][0]["image"].endswith("b" * 64)


@pytest.mark.parametrize("mutation", ["observed_generation", "replicas", "available", "endpoints",
    "unready_endpoint", "wrong_port", "wrong_uid", "wrong_ip", "pod_unready", "pod_revision",
    "pod_image", "pod_deleted", "cluster_ip"])
def test_readiness_never_follows_annotation_alone(configured, mutation):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    deployment = kube.object("Deployment")
    endpoint = kube.object("Endpoints")
    pod = kube.objects[("Pod", "test", "irc-pod")]
    if mutation == "observed_generation":
        deployment["status"]["observedGeneration"] = 0
    elif mutation == "replicas":
        deployment["status"]["updatedReplicas"] = 0
    elif mutation == "available":
        deployment["status"]["conditions"][0]["status"] = "False"
    elif mutation == "endpoints":
        del kube.objects[("Endpoints", "test", "irc-test")]
    elif mutation == "unready_endpoint":
        endpoint["subsets"][0]["notReadyAddresses"] = [{"ip": "10.0.1.6"}]
    elif mutation == "wrong_port":
        endpoint["subsets"][0]["ports"][0]["port"] = 6697
    elif mutation == "wrong_uid":
        endpoint["subsets"][0]["addresses"][0]["targetRef"]["uid"] = "wrong"
    elif mutation == "wrong_ip":
        endpoint["subsets"][0]["addresses"][0]["ip"] = "10.0.1.9"
    elif mutation == "pod_unready":
        pod["status"]["conditions"][0]["status"] = "False"
    elif mutation == "pod_revision":
        pod["metadata"]["annotations"]["cuse.io/irc-revision"] = "b" * 64
    elif mutation == "pod_image":
        pod["spec"]["containers"][0]["image"] = "wrong/image"
    elif mutation == "pod_deleted":
        pod["metadata"]["deletionTimestamp"] = "2026-10-18T00:00:00Z"
    elif mutation == "cluster_ip":
        del kube.object("Service")["spec"]["clusterIP"]
    observed = run(provider.observe(desired.identity))
    assert observed.exists and observed.revision == desired.revision and not observed.ready


@pytest.mark.parametrize("kind", ["Deployment", "Service"])
@pytest.mark.parametrize("change", ["foreign", "unowned", "owner_reference", "deleting"])
def test_collisions_preflight_every_object_and_never_adopt(configured, kind, change):
    kube, provider, desired = configured
    ensure(provider, desired)
    other = kube.object(kind)
    if change == "foreign":
        other["metadata"]["annotations"]["cuse.io/irc-owner"] = '["IRCServer","test","server","other-uid"]'
    elif change == "unowned":
        del other["metadata"]["annotations"]["cuse.io/irc-owner"]
    elif change == "owner_reference":
        other["metadata"]["ownerReferences"] = [{"uid": "another-owner"}]
    elif change == "deleting":
        other["metadata"]["deletionTimestamp"] = "2026-10-18T00:00:00Z"
    # Force work on the Deployment so a late Service collision must block it too.
    newer = plan(server_id="changed")
    before = deepcopy(kube.objects)
    count = len(kube.writes)
    with pytest.raises(TerminalProviderError):
        ensure(provider, newer)
    with pytest.raises(TerminalProviderError):
        run(provider.observe(desired.identity))
    assert kube.objects == before and len(kube.writes) == count


def test_uid_recreation_cannot_adopt_retained_workload(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    recreated = plan(uid="cr-new-uid")
    provider.bind_plan(recreated)
    with pytest.raises(TerminalProviderError):
        ensure(provider, recreated)
    assert len(kube.writes) == 2


def test_partial_create_unknown_outcome_reuses_names_and_key(configured):
    kube, provider, desired = configured
    kube.after_commit[("create", "Deployment")] = TimeoutError("must-not-leak")
    with pytest.raises(UncertainOutcome, match="did not complete reliably"):
        ensure(provider, desired)
    assert len(kube.objects) == 1
    assert run(provider.observe(desired.identity)).exists
    ensure(provider, desired)
    assert len(kube.objects) == 2
    assert [(v, k) for v, k, _ in kube.writes] == [("create", "Deployment"), ("create", "Service")]
    ensure(provider, desired)
    assert len(kube.writes) == 2


def test_unknown_patch_outcome_observation_prevents_duplicate_update(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    newer = plan(server_id="new")
    kube.after_commit[("patch", "Service")] = ApiException(status=503, reason="private error")
    with pytest.raises(UncertainOutcome):
        ensure(provider, newer)
    count = len(kube.writes)
    ensure(provider, newer)
    assert len(kube.writes) == count


@pytest.mark.parametrize("status, expected", [(403, TerminalProviderError),
    (429, RetryableProviderError), (500, RetryableProviderError)])
def test_read_errors_are_classified_without_exposing_responses(configured, status, expected):
    kube, provider, desired = configured
    kube.fail[("read", "Deployment")] = ApiException(status=status, reason="private response")
    with pytest.raises(expected) as error:
        run(provider.observe(desired.identity))
    assert "private" not in str(error.value)
    assert not kube.writes


def test_create_race_does_not_adopt(configured):
    kube, provider, desired = configured
    kube.fail[("create", "Deployment")] = ApiException(status=409)
    with pytest.raises(RetryableProviderError):
        ensure(provider, desired)
    assert len(kube.writes) == 1 and not kube.objects


def test_empty_configuration_never_reads_or_writes(configured):
    kube, _, desired = configured
    disabled = IRCServerKubernetesProvider(apps=kube, core=kube, settings={})
    with pytest.raises(ProviderNotConfigured):
        disabled.bind_plan(desired)
    with pytest.raises(ProviderNotConfigured):
        ensure(disabled, desired)
    with pytest.raises(ProviderNotConfigured):
        run(disabled.observe(desired.identity))
    assert not kube.calls


@pytest.mark.parametrize("field,value", [("image", "unverified:latest"),
    ("image", "registry.example.test/repo@sha256:wrong"), ("endpoint_mode", "tls"),
    ("service_name", "has.dot"), ("network_name", 'unsafe"/><oper'),
    ("cluster_domain", "cluster.local/unsafe")])
def test_invalid_settings_are_rejected_before_io(configured, field, value):
    kube, _, desired = configured
    provider = IRCServerKubernetesProvider(apps=kube, core=kube,
        settings={("test", "server"): replace(CONFIG, **{field: value})})
    with pytest.raises(TerminalProviderError):
        ensure(provider, desired)
    assert not kube.calls


@pytest.mark.parametrize("endpoint", ["ircs://irc-test.test.svc.cluster.local:6697",
    "irc://external.example:6667", "irc://irc-test.test.svc.cluster.local:6667/path",
    "irc://irc-test.test.svc.cluster.local:6667?mode=other"])
def test_unsupported_endpoint_never_gets_silently_reinterpreted(configured, endpoint):
    kube, provider, _ = configured
    with pytest.raises(TerminalProviderError):
        ensure(provider, plan(endpoint=endpoint))
    assert not kube.calls


def test_forged_revision_key_or_intent_is_rejected_before_io(configured):
    kube, provider, desired = configured
    with pytest.raises(TerminalProviderError):
        run(provider.ensure(desired, idempotency_key="wrong"))
    for altered in [replace(desired, revision="a" * 64),
                    replace(desired, idempotency_key="a" * 64)]:
        with pytest.raises(TerminalProviderError):
            ensure(provider, altered)
    body = {"kind": "IRCServer", "metadata": {"namespace": "test", "name": "server", "uid": "cr-uid-1"}}
    invalid_intent = deepcopy(desired.intent)
    invalid_intent["server"]["endpoint"] = "irc://wrong.example:6667"
    with pytest.raises(TerminalProviderError):
        ensure(provider, make_plan(body, invalid_intent))
    assert not kube.calls


def test_bind_copies_mutable_plan_input(configured):
    _, provider, desired = configured
    original = deepcopy(desired.intent)
    provider.bind_plan(desired)
    desired.intent["server"]["endpoint"] = "irc://wrong.example:6667"
    assert provider._plans[desired.identity].intent == original


def test_official_models_are_normalized_without_client_or_credentials():
    model = V1Deployment(api_version="apps/v1", kind="Deployment",
        metadata=V1ObjectMeta(name="test", namespace="test", uid="uid", resource_version="7",
                              generation=2, annotations={"key": "value"}))
    assert _wire(model) == {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "test", "namespace": "test", "uid": "uid",
                     "resourceVersion": "7", "generation": 2, "annotations": {"key": "value"}}}


def test_conditional_patch_race_is_retryable_and_never_overwrites_new_owner(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    newer = plan(server_id="new")
    def change_owner(body):
        body["metadata"]["uid"] = "recreated-workload"
        body["metadata"]["resourceVersion"] = "2"
        body["metadata"]["annotations"]["cuse.io/irc-owner"] = "foreign"
    kube.before_patch = change_owner
    with pytest.raises(RetryableProviderError):
        ensure(provider, newer)
    assert kube.object("Deployment")["metadata"]["annotations"]["cuse.io/irc-owner"] == "foreign"
    with pytest.raises(TerminalProviderError):
        ensure(provider, newer)


def test_kubernetes_omitted_empty_and_false_defaults_converge(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    def omit_defaults(body):
        if isinstance(body, dict):
            for key, value in list(body.items()):
                if value == [] or (value is False and key in {
                        "paused", "hostNetwork", "hostPID", "hostIPC", "privileged",
                        "readOnlyRootFilesystem", "publishNotReadyAddresses"}):
                    del body[key]
                else:
                    omit_defaults(value)
        elif isinstance(body, list):
            for item in body:
                omit_defaults(item)
    for body in kube.objects.values():
        omit_defaults(body)
    assert run(provider.observe(desired.identity)).ready
    ensure(provider, desired)
    assert len(kube.writes) == 2


def test_missing_security_opt_outs_are_drift_not_false_defaults(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    spec = kube.object("Deployment")["spec"]["template"]["spec"]
    del spec["automountServiceAccountToken"]
    del spec["containers"][0]["securityContext"]["allowPrivilegeEscalation"]
    assert not run(provider.observe(desired.identity)).ready
    ensure(provider, desired)
    kube.ready()
    assert run(provider.observe(desired.identity)).ready


def test_generated_kubernetes_clients_receive_valid_http_contracts():
    """Exercise installed API wrappers; replace only their network transport."""
    import json
    from kubernetes_asyncio.client import AppsV1Api, CoreV1Api

    kube = FakeKubernetes()

    class RecordingTransport:
        client_side_validation = True

        def __init__(self):
            self.requests = []

        def select_header_accept(self, accepts):
            return "application/json"

        def select_header_content_type(self, content_types, *args):
            return "application/json"

        async def call_api(self, path, method, path_params, query, headers, **kwargs):
            assert kwargs["_request_timeout"] == 30
            assert query == []
            assert kwargs["auth_settings"] == ["BearerToken"]
            body = json.loads(json.dumps(kwargs["body"]))
            self.requests.append((path, method, deepcopy(path_params), deepcopy(headers), body))
            kind = "Deployment" if "/deployments" in path else "Service"
            if method == "GET":
                return await kube.read(kind, **path_params)
            if method == "POST":
                # Generated POST wrappers defer JSON content type to ApiClient.
                assert headers.get("Content-Type") in {None, "application/json"}
                return await kube.create(kind, namespace=path_params["namespace"], body=body)
            assert method == "PATCH"
            assert headers["Content-Type"] == "application/json-patch+json"
            return await kube.patch(kind, **path_params, body=body,
                                    _content_type=headers["Content-Type"])

    transport = RecordingTransport()
    provider = IRCServerKubernetesProvider(
        apps=AppsV1Api(api_client=transport), core=CoreV1Api(api_client=transport),
        settings={("test", "server"): CONFIG})
    desired = plan()
    ensure(provider, desired)
    ensure(provider, plan(server_id="updated"))
    assert [method for _, method, _, _, _ in transport.requests].count("POST") == 2
    patches = [request for request in transport.requests if request[1] == "PATCH"]
    assert len(patches) == 2
    assert patches[0][0] == "/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
    assert patches[1][0] == "/api/v1/namespaces/{namespace}/services/{name}"
    for _, _, params, _, body in patches:
        assert params == {"namespace": "test", "name": "irc-test"}
        assert body[0]["op"] == body[1]["op"] == "test"


@pytest.mark.parametrize("which", ["selector", "headless"])
def test_immutable_conflicts_retain_everything_without_writes(configured, which):
    kube, provider, desired = configured
    ensure(provider, desired)
    if which == "selector":
        kube.object("Deployment")["spec"]["selector"]["matchLabels"] = {"wrong": "selector"}
    else:
        kube.object("Service")["spec"]["clusterIP"] = "None"
    previous = deepcopy(kube.objects)
    with pytest.raises(TerminalProviderError):
        ensure(provider, desired)
    assert len(kube.writes) == 2
    assert kube.objects == previous


@pytest.mark.parametrize("field,value", [("runAsUser", 0), ("runAsGroup", 0),
    ("runAsNonRoot", False), ("seccompProfile", {"type": "Unconfined"})])
@pytest.mark.parametrize("target", ["deployment", "pod"])
def test_container_security_overrides_cannot_hide_behind_pod_defaults(configured, field, value, target):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    if target == "deployment":
        spec = kube.object("Deployment")["spec"]["template"]["spec"]
    else:
        spec = kube.objects[("Pod", "test", "irc-pod")]["spec"]
    spec["containers"][0]["securityContext"][field] = value
    observed = run(provider.observe(desired.identity))
    assert not observed.ready
    if target == "deployment":
        assert observed.revision != desired.revision
        ensure(provider, desired)
        kube.ready()
        assert run(provider.observe(desired.identity)).ready


@pytest.mark.parametrize("field", ["hostPort", "hostIP", "readiness_host", "startup_host"])
@pytest.mark.parametrize("target", ["deployment", "pod"])
def test_network_and_probe_overrides_are_not_ignored(configured, field, target):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    if target == "deployment":
        spec = kube.object("Deployment")["spec"]["template"]["spec"]
    else:
        spec = kube.objects[("Pod", "test", "irc-pod")]["spec"]
    container = spec["containers"][0]
    if field in {"hostPort", "hostIP"}:
        container["ports"][0][field] = 6667 if field == "hostPort" else "0.0.0.0"
    else:
        probe = "readinessProbe" if field == "readiness_host" else "startupProbe"
        container[probe]["tcpSocket"]["host"] = "different-service"
    assert not run(provider.observe(desired.identity)).ready
    if target == "deployment":
        ensure(provider, desired)
        kube.ready()
        assert run(provider.observe(desired.identity)).ready


def test_omitted_empty_network_fields_match_their_actual_api_defaults(configured):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    for spec in (kube.object("Deployment")["spec"]["template"]["spec"],
                 kube.objects[("Pod", "test", "irc-pod")]["spec"]):
        container = spec["containers"][0]
        del container["ports"][0]["hostIP"]
        del container["ports"][0]["hostPort"]
        del container["readinessProbe"]["tcpSocket"]["host"]
        del container["startupProbe"]["tcpSocket"]["host"]
    assert run(provider.observe(desired.identity)).ready
    ensure(provider, desired)
    assert len(kube.writes) == 2


@pytest.mark.parametrize("field", ["postStart", "preStop", "livenessProbe"])
@pytest.mark.parametrize("target", ["deployment", "pod"])
def test_unrequested_execution_hooks_and_liveness_are_rejected(configured, field, target):
    kube, provider, desired = configured
    ensure(provider, desired)
    kube.ready()
    if target == "deployment":
        spec = kube.object("Deployment")["spec"]["template"]["spec"]
    else:
        spec = kube.objects[("Pod", "test", "irc-pod")]["spec"]
    container = spec["containers"][0]
    if field == "livenessProbe":
        container[field] = {"exec": {"command": ["/bin/sh", "-c", "exit 0"]}}
    else:
        container["lifecycle"] = {field: {"exec": {"command": ["/bin/sh", "-c", "exit 0"]}}}
    assert not run(provider.observe(desired.identity)).ready
    if target == "deployment":
        ensure(provider, desired)
        updated = kube.object("Deployment")["spec"]["template"]["spec"]["containers"][0]
        assert updated.get("lifecycle") is None
        assert updated.get("livenessProbe") is None
        # A healthy old Pod still carrying the hook cannot establish readiness.
        assert not run(provider.observe(desired.identity)).ready
        kube.ready()
        assert run(provider.observe(desired.identity)).ready
        writes = len(kube.writes)
        ensure(provider, desired)
        assert len(kube.writes) == writes
