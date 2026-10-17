"""Exercise independent event handlers with a fake namespace-scoped API."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import kopf

from cuse_operator.operator import DEPENDENCY_WATCHES, DependencyValidator
from cuse_operator.operator import ALL_PLURALS as PLURALS


def resource(kind, name, spec, namespace="default", uid=None):
    return {"apiVersion": "cuse.io/v1alpha1", "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "uid": uid or name,
                         "resourceVersion": "1", "generation": 1}, "spec": spec}


def server(namespace="default", uid="server"):
    return resource("IRCServer", "server", {"id": "irc", "endpoint": "ircs://irc.example.test"},
                    namespace, uid)


def weechat(namespace="default", pinned=False):
    ref = {"name": "server"}
    if pinned:
        ref["uid"] = "server"
    return resource("WeeChat", "chat", {"id": "chat-id", "serverRef": ref,
                    "nick": "bot", "mcp_endpoint": "https://mcp.example.test"}, namespace)


class FakeAPI:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.patches = []
        self.reads = []
        self.conflict = False

    async def list_namespaced_custom_object(self, group, version, namespace, plural, **kwargs):
        assert 0 < kwargs["_request_timeout"] <= 30
        self.reads.append((namespace, plural))
        return {"items": deepcopy([body for body in self.bodies
                if body["metadata"]["namespace"] == namespace and PLURALS[body["kind"]] == plural])}

    async def patch_namespaced_custom_object_status(self, group, version, namespace, plural,
                                                   name, patch, **kwargs):
        assert 0 < kwargs["_request_timeout"] <= 30
        assert kwargs["_content_type"] == "application/merge-patch+json"
        self.patches.append((namespace, plural, name, deepcopy(patch)))
        if self.conflict:
            error = RuntimeError("conflict")
            error.status = 409
            raise error
        body = next(body for body in self.bodies if body["metadata"]["name"] == name
                    and body["metadata"]["namespace"] == namespace
                    and PLURALS[body["kind"]] == plural)
        assert patch["metadata"] == {key: body["metadata"][key] for key in ("uid", "resourceVersion")}
        body["status"] = deepcopy(patch["status"])
        body["metadata"]["resourceVersion"] = str(int(body["metadata"]["resourceVersion"]) + 1)


def test_dependency_creation_edit_deletion_and_status_only_events():
    async def scenario():
        chat = weechat()
        api = FakeAPI(chat)
        controller = DependencyValidator(api)
        memo = SimpleNamespace(dependency_validator=controller)
        await DEPENDENCY_WATCHES["WeeChat"](event={"type": None, "object": chat}, memo=memo)
        assert chat["status"]["reason"] == "MissingDependency"
        irc = server()
        api.bodies.append(irc)
        await controller.event("IRCServer", {"type": "ADDED", "object": irc})
        assert chat["status"]["phase"] == "Valid"
        reads = len(api.reads)
        await controller.event("IRCServer", {"type": "MODIFIED", "object": irc})
        assert len(api.reads) == reads
        irc["spec"]["endpoint"] = "invalid"
        irc["metadata"]["generation"] = 2
        await controller.event("IRCServer", {"type": "MODIFIED", "object": irc})
        assert chat["status"]["phase"] != "Valid"
        api.bodies.remove(irc)
        await controller.event("IRCServer", {"type": "DELETED", "object": irc})
        assert chat["status"]["reason"] == "MissingDependency"
        assert all(set(patch) == {"metadata", "status"} for *_, patch in api.patches)
    asyncio.run(scenario())


def test_recreation_rebinds_only_unpinned_references():
    async def scenario():
        pinned, unpinned = weechat(pinned=True), weechat()
        unpinned["metadata"]["name"] = "chat-two"
        unpinned["spec"]["id"] = "chat-two-id"
        api = FakeAPI(server(uid="replacement"), pinned, unpinned)
        await DependencyValidator(api).refresh("default")
        assert pinned["status"]["reason"] == "ReferenceMismatch"
        assert unpinned["status"]["phase"] == "Valid"
    asyncio.run(scenario())


def test_duplicates_and_namespace_isolation():
    async def scenario():
        first, duplicate, elsewhere = server(), server(), server("other")
        duplicate["metadata"]["name"] = "second-server"
        api = FakeAPI(first, duplicate, elsewhere, weechat("other"))
        controller = DependencyValidator(api)
        await controller.refresh("default")
        assert first["status"]["reason"] == duplicate["status"]["reason"] == "DuplicateIdentity"
        assert "status" not in elsewhere
        await controller.refresh("other")
        assert elsewhere["status"]["phase"] == "Valid"
        api.bodies.remove(duplicate)
        await controller.event("IRCServer", {"type": "DELETED", "object": duplicate})
        assert first["status"]["phase"] == "Valid"
    asyncio.run(scenario())


def test_conflict_recovers_from_fresh_periodic_read():
    async def scenario():
        irc = server()
        api = FakeAPI(irc)
        api.conflict = True
        controller = DependencyValidator(api)
        await controller.event("IRCServer", {"type": "ADDED", "object": irc})
        assert "status" not in irc
        api.conflict = False
        irc["metadata"]["resourceVersion"] = "2"
        task = asyncio.create_task(controller.resync(interval=0.001))
        try:
            async with asyncio.timeout(1):
                while "status" not in irc:
                    await asyncio.sleep(0.001)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert irc["status"]["phase"] == "Valid"
        assert api.patches[-1][-1]["metadata"]["resourceVersion"] == "2"
    asyncio.run(scenario())


def test_all_kinds_have_raw_watches_without_finalizers():
    assert set(DEPENDENCY_WATCHES) == set(PLURALS)
    registry = kopf.get_default_registry()
    watchers = registry._watching._handlers
    assert {str(handler.id) for handler in watchers} >= {
        f"reconcile-{plural}" for plural in PLURALS.values()}
    assert not registry._spawning._handlers


def test_public_async_client_uses_status_endpoint_and_merge_patch():
    from kubernetes_asyncio import client

    async def scenario():
        api_client = client.ApiClient()
        calls = []

        async def call_api(path, method, path_params, query_params, headers, **kwargs):
            calls.append((path, method, path_params, headers, kwargs))
            return {}

        api_client.call_api = call_api
        api = client.CustomObjectsApi(api_client)
        try:
            await api.patch_namespaced_custom_object_status(
                "cuse.io", "v1alpha1", "default", "ircservers", "server",
                {"metadata": {"uid": "u", "resourceVersion": "1"}, "status": {}},
                _content_type="application/merge-patch+json", _request_timeout=30,
            )
        finally:
            await api_client.close()
        path, method, params, headers, kwargs = calls[0]
        assert path.endswith("/{name}/status")
        assert method == "PATCH"
        assert headers["Content-Type"] == "application/merge-patch+json"
        assert params["namespace"] == "default"
        assert kwargs["body"]["metadata"] == {"uid": "u", "resourceVersion": "1"}
    asyncio.run(scenario())


def test_event_errors_are_redacted_and_cancellation_is_not_swallowed(caplog):
    class FailingValidator:
        def __init__(self, error):
            self.error = error

        async def event(self, *args):
            raise self.error

    async def scenario():
        memo = SimpleNamespace(dependency_validator=FailingValidator(
            RuntimeError("sensitive-response-body")))
        await DEPENDENCY_WATCHES["IRCServer"](event={}, memo=memo)
        assert "retrying on periodic resync" in caplog.text
        assert "sensitive-response-body" not in caplog.text
        memo.dependency_validator = FailingValidator(asyncio.CancelledError())
        try:
            await DEPENDENCY_WATCHES["IRCServer"](event={}, memo=memo)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancellation was swallowed")
    asyncio.run(scenario())
