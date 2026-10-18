import asyncio
from copy import deepcopy

from cuse_operator.bot_provider import BotBindingProvider
from cuse_operator.controllers import CONTROLLERS
from cuse_operator.controllers.base import identity
from cuse_operator.provider_router import ProviderRouter
from cuse_operator.providers import InMemoryProvider
from test_resources import graph  # noqa: F401


def test_bot_observes_composition_without_an_extra_workload(graph):  # noqa: F811
    runtime = InMemoryProvider()
    router = ProviderRouter({kind: runtime for kind in CONTROLLERS if kind != "Bot"})
    router.providers["Bot"] = BotBindingProvider(router)
    for body in graph:
        assert asyncio.run(CONTROLLERS[body["kind"]].reconcile(body, graph, router))["ready"]
    assert all(key.kind != "Bot" for key in runtime.plans)
    runtime.set_ready(identity(graph[4]), False)
    assert not asyncio.run(CONTROLLERS["Bot"].reconcile(graph[6], graph, router))["ready"]
    before = deepcopy(runtime.state)
    asyncio.run(CONTROLLERS["Bot"].on_delete(graph[6], router))
    assert runtime.state == before
