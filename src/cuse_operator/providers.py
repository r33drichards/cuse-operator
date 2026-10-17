"""Async infrastructure boundary. No network provider is shipped.

Providers must key state by the complete ResourceIdentity (including UID), never
adopt another UID, and make ensure idempotent across restarts using the supplied
key. ensure is an upsert of desired intent, not an imperative create command.
After unknown outcomes callers re-observe first; implementations must also dedupe
repeated keys when observation is eventually consistent. There is no delete API:
external/shared state is deliberately retained.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol
from copy import deepcopy


@dataclass(frozen=True)
class ResourceIdentity:
    kind: str
    namespace: str
    name: str
    uid: str


@dataclass(frozen=True)
class DesiredPlan:
    identity: ResourceIdentity
    intent: dict[str, Any]
    revision: str
    idempotency_key: str


@dataclass(frozen=True)
class Observation:
    exists: bool = False
    revision: str = ""
    ready: bool = False


class RetryableProviderError(Exception):
    """Transient failure; safe to retry after observing."""


class TerminalProviderError(Exception):
    """Provider configuration or intent cannot be applied."""


class UncertainOutcome(RetryableProviderError):
    """Apply may have committed: observe and reuse the same key on retry."""


class ProviderNotConfigured(TerminalProviderError):
    pass


class Provider(Protocol):
    async def observe(self, identity: ResourceIdentity) -> Observation: ...
    async def ensure(self, desired: DesiredPlan, *, idempotency_key: str) -> None: ...


class DisabledProvider:
    async def observe(self, identity: ResourceIdentity) -> Observation:
        raise ProviderNotConfigured()

    async def ensure(self, desired: DesiredPlan, *, idempotency_key: str) -> None:
        raise ProviderNotConfigured()


class InMemoryProvider:
    """Test utility only; convergence does not indicate real infrastructure.

    Set auto_ready=False to simulate asynchronous provisioning; set_ready and
    drift simulate provider state changes. UID recreation leaves old state alone.
    """
    def __init__(self, *, auto_ready: bool = True):
        self.auto_ready = auto_ready
        self.state: dict[ResourceIdentity, Observation] = {}
        self.plans: dict[ResourceIdentity, DesiredPlan] = {}
        self.applied_keys: list[str] = []
        self.observe_count = 0
        self.ensure_count = 0

    async def observe(self, identity: ResourceIdentity) -> Observation:
        self.observe_count += 1
        return self.state.get(identity, Observation())

    async def ensure(self, desired: DesiredPlan, *, idempotency_key: str) -> None:
        if idempotency_key != desired.idempotency_key:
            raise TerminalProviderError()
        current = self.state.get(desired.identity, Observation())
        if current.exists and current.revision == desired.revision:
            return
        self.ensure_count += 1
        self.applied_keys.append(idempotency_key)
        self.plans[desired.identity] = deepcopy(desired)
        self.state[desired.identity] = Observation(True, desired.revision, self.auto_ready)

    def set_ready(self, identity: ResourceIdentity, ready: bool = True) -> None:
        current = self.state[identity]
        self.state[identity] = Observation(current.exists, current.revision, ready)

    def drift(self, identity: ResourceIdentity, revision: str = "drifted") -> None:
        self.state[identity] = Observation(True, revision, False)
