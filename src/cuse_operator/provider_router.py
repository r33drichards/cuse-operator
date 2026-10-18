"""Explicit per-kind provider dispatch, with fail-closed defaults.

Adapters may bind a desired plan before observation, allowing a fresh process to
compare live managed fields with desired fields without trusting annotations or
an earlier ensure call. Binding is synchronous and must perform no I/O.
"""
from collections.abc import Mapping

from .providers import DesiredPlan, DisabledProvider, Provider, ResourceIdentity


class ProviderRouter:
    def __init__(self, providers: Mapping[str, Provider]):
        self.providers = dict(providers)
        self.disabled = DisabledProvider()

    def _provider(self, identity: ResourceIdentity):
        return self.providers.get(identity.kind, self.disabled)

    def bind_plan(self, desired: DesiredPlan) -> None:
        binder = getattr(self._provider(desired.identity), "bind_plan", None)
        if binder is not None:
            binder(desired)

    async def observe(self, identity: ResourceIdentity):
        return await self._provider(identity).observe(identity)

    async def ensure(self, desired: DesiredPlan, *, idempotency_key: str) -> None:
        await self._provider(desired.identity).ensure(desired, idempotency_key=idempotency_key)
