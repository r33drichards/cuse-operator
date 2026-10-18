import asyncio

import pytest

from cuse_operator.controllers import CONTROLLERS
from cuse_operator.provider_router import ProviderRouter
from cuse_operator.providers import InMemoryProvider, ProviderNotConfigured, ResourceIdentity
from test_resources import graph  # noqa: F401


def run(value):
    return asyncio.run(value)


def test_unknown_kind_is_disabled():
    router = ProviderRouter({})
    with pytest.raises(ProviderNotConfigured):
        run(router.observe(ResourceIdentity("Session", "n", "s", "uid")))


def test_routes_each_kind_and_binds_transitive_plans(graph):  # noqa: F811
    class Bound(InMemoryProvider):
        def __init__(self):
            super().__init__()
            self.bound = set()

        def bind_plan(self, plan):
            self.bound.add(plan.identity)

        async def observe(self, identity):
            assert identity in self.bound
            return await super().observe(identity)

    adapters = {kind: Bound() for kind in CONTROLLERS}
    router = ProviderRouter(adapters)
    for body in graph:
        result = run(CONTROLLERS[body["kind"]].reconcile(body, graph, router))
        assert result["ready"]
    for kind, adapter in adapters.items():
        assert all(identity.kind == kind for identity in adapter.bound)
    assert len(adapters["IRCServer"].bound) == 1
    assert adapters["IRCServer"].ensure_count == 1


def test_binding_failure_is_redacted(graph):  # noqa: F811
    class Broken(InMemoryProvider):
        def bind_plan(self, plan):
            raise ValueError("private provider configuration")

    result = run(CONTROLLERS["IRCServer"].reconcile(graph[0], graph, Broken()))
    assert not result["ready"]
    assert result["reconciliationReason"] == "ProviderError"
    assert "private" not in str(result)
