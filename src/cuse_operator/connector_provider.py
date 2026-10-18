"""Observe a Connector bound to an independently managed WeeChat runtime.

This adapter never creates a second IRC client. Generic/external Connectors need
an explicit runtime/health adapter and remain unconfigured here. MCP handshake
and authenticated connectivity are the owning runtime adapter's responsibility.
"""
from copy import deepcopy

from .providers import (
    DesiredPlan, Observation, ProviderNotConfigured, ResourceIdentity, TerminalProviderError,
)


class WeeChatConnectorProvider:
    def __init__(self, weechat_provider):
        self.weechat_provider = weechat_provider
        self.plans = {}

    def bind_plan(self, desired: DesiredPlan) -> None:
        if desired.identity.kind != "Connector":
            raise TerminalProviderError()
        self.plans[desired.identity] = deepcopy(desired)

    def _binding(self, identity):
        plan = self.plans.get(identity)
        if plan is None or "weechatRef" not in plan.intent.get("spec", {}):
            raise ProviderNotConfigured()
        bindings = [d for d in plan.intent["dependencies"] if d["kind"] == "WeeChat"]
        if len(bindings) != 1:
            raise TerminalProviderError()
        return plan, bindings[0]

    async def observe(self, identity: ResourceIdentity) -> Observation:
        plan, binding = self._binding(identity)
        target = ResourceIdentity("WeeChat", binding["namespace"], binding["name"], binding["uid"])
        state = await self.weechat_provider.observe(target)
        ready = state.exists and state.ready and state.revision == binding["desiredRevision"]
        # Existence is the logical binding, not another provisioned workload.
        return Observation(True, plan.revision, ready)

    async def ensure(self, desired: DesiredPlan, *, idempotency_key: str) -> None:
        if idempotency_key != desired.idempotency_key:
            raise TerminalProviderError()
        self.bind_plan(desired)
        self._binding(desired.identity)
        # Deliberately no side effects: owner readiness is re-observed.
