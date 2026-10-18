"""Reconcile a Bot's live pair of WeeChat and PiHarness dependencies.

A Bot is a logical composition, not a second process. This provider observes the
independently managed runtime identities and never owns or deletes their state.
"""
from copy import deepcopy

from .providers import Observation, ProviderNotConfigured, ResourceIdentity, TerminalProviderError


class BotBindingProvider:
    def __init__(self, provider):
        self.provider = provider
        self.plans = {}

    def bind_plan(self, desired):
        dependencies = desired.intent.get("dependencies", [])
        if (desired.identity.kind != "Bot" or len(dependencies) != 2
                or {d.get("kind") for d in dependencies} != {"WeeChat", "PiHarness"}):
            raise TerminalProviderError()
        self.plans[desired.identity] = deepcopy(desired)

    async def observe(self, identity):
        desired = self.plans.get(identity)
        if desired is None:
            raise ProviderNotConfigured()
        ready = True
        for dep in desired.intent["dependencies"]:
            observed = await self.provider.observe(ResourceIdentity(dep["kind"], dep["namespace"], dep["name"], dep["uid"]))
            ready = ready and observed.exists and observed.ready and observed.revision == dep["desiredRevision"]
        return Observation(True, desired.revision, ready)

    async def ensure(self, desired, *, idempotency_key):
        if idempotency_key != desired.idempotency_key:
            raise TerminalProviderError()
        self.bind_plan(desired)
