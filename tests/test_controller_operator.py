"""Actual all-kind controller dispatch, with fake Kubernetes and provider state."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from cuse_operator.controllers import CONTROLLERS
from cuse_operator.operator import ALL_PLURALS, ControllerManager, DEPENDENCY_WATCHES
from cuse_operator.providers import DisabledProvider, InMemoryProvider
from cuse_operator.schema import build_crds
from test_split_operator import FakeAPI, resource, server


def graph():
    root = Path(__file__).parents[1]
    bodies = json.loads((root / "examples/resources.json").read_text())["items"]
    bodies.append(resource("Architecture", "architecture", json.loads(
        (root / "examples/architecture.json").read_text())))
    for body in bodies:
        body["metadata"].update(uid=body["metadata"]["name"] + "-uid",
                                resourceVersion="1", generation=1)
    return bodies


def test_all_eight_active_controllers_write_typed_disabled_status():
    async def run():
        bodies = graph()
        api = FakeAPI(*bodies)
        manager = ControllerManager(api)
        assert isinstance(manager.provider, DisabledProvider)
        assert set(CONTROLLERS) == set(ALL_PLURALS) == set(DEPENDENCY_WATCHES)
        await manager.refresh("default")
        assert {plural for _, plural in api.reads} == set(ALL_PLURALS.values())
        schemas = {crd["spec"]["names"]["kind"]: crd["spec"]["versions"][0]
                   ["schema"]["openAPIV3Schema"]["properties"]["status"]
                   for crd in build_crds()["items"]}
        for body in bodies:
            status = body["status"]
            assert status["phase"] == "Valid"
            assert status["ready"] is False
            assert status["reconciliationReason"] == "ProviderNotConfigured"
            jsonschema.validate(status, schemas[body["kind"]])
    asyncio.run(run())


def test_actual_provider_dispatch_converges_and_restart_is_idempotent():
    async def run():
        bodies, provider = graph(), InMemoryProvider()
        api = FakeAPI(*bodies)
        await ControllerManager(api, provider).refresh("default")
        assert provider.ensure_count == 8
        assert all(body["status"]["ready"] for body in bodies)
        await ControllerManager(api, provider).refresh("default")
        assert provider.ensure_count == 8
        assert all(body["status"]["lastAction"] == "NoOp" for body in bodies)
        bodies[0]["spec"]["endpoint"] = "ircs://new.example.test"
        bodies[0]["metadata"]["generation"] += 1
        await ControllerManager(api, provider).refresh("default")
        assert provider.ensure_count > 8
        assert all(body["status"]["ready"] for body in bodies)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ALL_PLURALS)
def test_each_kind_receives_resume_create_update_and_delete(kind):
    class Spy:
        def __init__(self):
            self.reconciled = []
            self.deleted = []

        async def reconcile(self, body, inventory, provider):
            self.reconciled.append(deepcopy(body))
            return {"phase": "Valid", "reason": "Validated", "message": "Dispatched",
                    "observedGeneration": body["metadata"]["generation"], "ready": False}

        async def on_delete(self, body, provider):
            self.deleted.append(deepcopy(body))
            return {}

    async def run():
        body = next(body for body in graph() if body["kind"] == kind)
        spies = {kind: Spy() for kind in ALL_PLURALS}
        api = FakeAPI(body)
        manager = ControllerManager(api, controllers=spies)
        memo = SimpleNamespace(dependency_validator=manager)
        watch = DEPENDENCY_WATCHES[kind]
        await watch(event={"type": None, "object": body}, memo=memo)
        assert len(spies[kind].reconciled) == 1
        await watch(event={"type": "MODIFIED", "object": body}, memo=memo)
        assert len(spies[kind].reconciled) == 1  # status-only
        body["metadata"]["generation"] = 2
        await watch(event={"type": "MODIFIED", "object": body}, memo=memo)
        assert len(spies[kind].reconciled) == 2
        body["metadata"]["uid"] = "replacement"
        await watch(event={"type": "ADDED", "object": body}, memo=memo)
        assert len(spies[kind].reconciled) == 3
        body["metadata"]["deletionTimestamp"] = "2026-10-17T00:00:00Z"
        await watch(event={"type": "MODIFIED", "object": body}, memo=memo)
        assert len(spies[kind].deleted) == 1
        api.bodies.clear()
        await watch(event={"type": "DELETED", "object": body}, memo=memo)
        assert len(spies[kind].deleted) == 2
        assert all(not spy.deleted for other, spy in spies.items() if other != kind)
    asyncio.run(run())


def test_delete_retains_provider_resources_and_shared_dependencies():
    async def run():
        bodies, provider = graph(), InMemoryProvider()
        api = FakeAPI(*bodies)
        manager = ControllerManager(api, provider)
        await manager.refresh("default")
        original = deepcopy(provider.state)
        removed = api.bodies.pop(1)
        await manager.event("WeeChat", {"type": "DELETED", "object": removed})
        assert provider.state == original
        assert len(provider.state) == 8
    asyncio.run(run())


def test_provider_timeout_is_bounded_and_periodic_retry_is_possible():
    class Slow:
        async def reconcile(self, *args):
            await asyncio.sleep(10)

    async def run():
        api = FakeAPI(server())
        manager = ControllerManager(api, controllers={"IRCServer": Slow()}, timeout=0.001)
        await manager.refresh("default")
        assert api.bodies[0]["status"]["ready"] is False
        assert api.bodies[0]["status"]["reconciliationReason"] == "RetryableProviderError"
        manager.controllers = CONTROLLERS
        await manager.refresh("default")
        assert api.bodies[0]["status"]["reconciliationReason"] == "ProviderNotConfigured"
    asyncio.run(run())


def test_failed_delete_callback_is_retried_on_refresh_without_body():
    class RetryDelete:
        calls = 0

        async def on_delete(self, body, provider):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("sensitive provider body")
            return {}

    async def run():
        callback = RetryDelete()
        aggregate = graph()[-1]
        manager = ControllerManager(FakeAPI(aggregate),
                                    controllers={**CONTROLLERS, "IRCServer": callback})
        await manager.event("IRCServer", {"type": "DELETED", "object": server()})
        assert len(manager.pending_deletes) == 1
        assert aggregate["status"]["reconciliationReason"] == "ProviderNotConfigured"
        await manager.refresh("default")
        assert callback.calls == 2
        assert not manager.pending_deletes
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_failed_first_controller_clears_ready_and_does_not_starve_aggregate(failure):
    class Failing:
        async def reconcile(self, *args):
            if failure == "timeout":
                await asyncio.sleep(10)
            raise RuntimeError("sensitive provider payload")

    async def run():
        bodies = [graph()[0], graph()[-1]]
        bodies[0]["status"] = {"ready": True}
        api = FakeAPI(*bodies)
        controllers = {**CONTROLLERS, "IRCServer": Failing()}
        manager = ControllerManager(api, controllers=controllers, timeout=0.01)
        for _ in range(2):
            await manager.refresh("default")
            assert bodies[0]["status"]["ready"] is False
            assert bodies[0]["status"]["reconciliationReason"] == (
                "RetryableProviderError" if failure == "timeout" else "ProviderError")
            assert bodies[-1]["status"]["reconciliationReason"] == "ProviderNotConfigured"
            assert "sensitive" not in json.dumps([body["status"] for body in bodies])
    asyncio.run(run())
