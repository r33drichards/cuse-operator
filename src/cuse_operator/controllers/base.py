"""Shared reconciliation engine; each kind supplies its own desired plan."""
from __future__ import annotations
from copy import deepcopy
import asyncio
import hashlib
import json
import re
from typing import Callable, Literal, TypedDict

from ..providers import (DesiredPlan, ResourceIdentity, DisabledProvider, Provider,
    ProviderNotConfigured, RetryableProviderError, TerminalProviderError, UncertainOutcome)
from ..resources import validate_resource


class ReconciliationStatus(TypedDict):
    phase: Literal["Valid", "Invalid", "Blocked"]
    reason: str
    message: str
    ready: bool
    reconciliationReason: str
    desiredRevision: str
    observedRevision: str
    lastAction: str
    observedGeneration: int


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def identity(body) -> ResourceIdentity:
    meta = body["metadata"]
    values = [body["kind"], meta["namespace"], meta["name"], meta["uid"]]
    if any(type(value) is not str or not value for value in values):
        raise ValueError("Invalid identity")
    return ResourceIdentity(*values)


def make_plan(body, intent) -> DesiredPlan:
    ident = identity(body)
    revision = digest(intent)
    key = digest([ident.kind, ident.namespace, ident.name, ident.uid, revision])
    return DesiredPlan(ident, deepcopy(intent), revision, key)


class PlanContext:
    """Resolve validated references through each dependency's own plan builder.

    Dependency plan digests include all their dependencies, so transitive edits
    affect downstream desired revisions without embedding unrelated inventory.
    """
    def __init__(self, namespace, inventory):
        self.namespace = namespace
        self.index = {(b.get("kind"), b.get("metadata", {}).get("namespace"),
                       b.get("metadata", {}).get("name")): b for b in inventory}
        self.cache = {}

    def dependency(self, kind, ref):
        from . import CONTROLLERS
        key = (kind, self.namespace, ref["name"])
        body = self.index[key]
        if body["metadata"].get("deletionTimestamp"):
            raise ValueError("Deleting dependency")
        if ref.get("uid", body["metadata"].get("uid")) != body["metadata"].get("uid"):
            raise ValueError("UID mismatch")
        if key not in self.cache:
            self.cache[key] = CONTROLLERS[kind].build_plan(body, self)
        plan = self.cache[key]
        return {"kind": kind, "namespace": plan.identity.namespace,
                "name": plan.identity.name, "uid": plan.identity.uid,
                "specDigest": digest(body["spec"]), "desiredRevision": plan.revision}


class Controller:
    def __init__(self, kind: str, build_plan: Callable, validator: Callable | None = None, *, provider_timeout: float = 30):
        self.kind, self.build_plan, self.validator = kind, build_plan, validator
        self.provider_timeout = provider_timeout

    async def reconcile(self, body, inventory, provider: Provider | None = None) -> ReconciliationStatus:
        inventory = list(inventory)
        generation = body.get("metadata", {}).get("generation", 0)
        status = {"phase": "Invalid", "reason": "ValidationFailed",
                  "message": "Resource or dependency specification is invalid.",
                  "ready": False, "reconciliationReason": "ValidationFailed",
                  "desiredRevision": "", "observedRevision": "", "lastAction": "None",
                  "observedGeneration": generation}
        try:
            if body.get("kind") != self.kind:
                return status
            if self.validator:
                validation = self.validator(body)
            else:
                validation = validate_resource(self.kind, body.get("spec"),
                    body.get("metadata", {}).get("namespace"), inventory)
            status.update(validation)
            status["reconciliationReason"] = validation["reason"]
            if status["phase"] != "Valid":
                return status
            context = PlanContext(body["metadata"]["namespace"], inventory)
            desired = self.build_plan(body, context)
        except (KeyError, TypeError, ValueError, RecursionError):
            status.update(phase="Invalid", reason="ValidationFailed", reconciliationReason="ValidationFailed",
                          message="Resource or dependency specification is invalid.")
            return status
        status["desiredRevision"] = desired.revision
        provider = provider if provider is not None else DisabledProvider()
        try:
            observed = await asyncio.wait_for(provider.observe(desired.identity), timeout=self.provider_timeout)
            status["observedRevision"] = observed.revision if re.fullmatch(r"[0-9a-f]{64}", observed.revision) else ""
            # Every transitive dependency is checked: ancestor readiness can
            # change without a spec revision and must still gate child readiness.
            for dependency_plan in context.cache.values():
                dependency_state = await asyncio.wait_for(provider.observe(dependency_plan.identity),
                                                          timeout=self.provider_timeout)
                if (not dependency_state.exists or not dependency_state.ready
                        or dependency_state.revision != dependency_plan.revision):
                    status.update(reconciliationReason="DependencyNotReady",
                                  message="Waiting for a required dependency to converge and become ready.")
                    return status
            if not observed.exists or observed.revision != desired.revision:
                status["lastAction"] = "Update" if observed.exists else "Create"
                await asyncio.wait_for(provider.ensure(desired, idempotency_key=desired.idempotency_key),
                                       timeout=self.provider_timeout)
                observed = await asyncio.wait_for(provider.observe(desired.identity), timeout=self.provider_timeout)
                status["observedRevision"] = observed.revision if re.fullmatch(r"[0-9a-f]{64}", observed.revision) else ""
            else:
                status["lastAction"] = "NoOp"
            status["ready"] = observed.exists and observed.ready and observed.revision == desired.revision
            status["reconciliationReason"] = "Ready" if status["ready"] else "Pending"
            status["message"] = "Desired state is observed and ready." if status["ready"] else "Waiting for provider convergence and readiness."
        except ProviderNotConfigured:
            status.update(reconciliationReason="ProviderNotConfigured", message="No infrastructure provider is configured.")
        except UncertainOutcome:
            status.update(reconciliationReason="UncertainOutcome", message="Provider outcome is unknown; observation will precede retry.")
        except (RetryableProviderError, TimeoutError):
            status.update(reconciliationReason="RetryableProviderError", message="A temporary provider failure requires retry.")
        except TerminalProviderError:
            status.update(reconciliationReason="TerminalProviderError", message="Provider cannot apply the requested intent.")
        except Exception:
            status.update(reconciliationReason="ProviderError", message="Provider reconciliation failed.")
        return status

    async def on_delete(self, body, provider: Provider | None = None) -> ReconciliationStatus:
        return {"phase": "Valid", "reason": "Retained", "message": "External and shared state is retained; no deletion was attempted.",
                "ready": False, "reconciliationReason": "Retained", "desiredRevision": "", "observedRevision": "",
                "lastAction": "Retain", "observedGeneration": body.get("metadata", {}).get("generation", 0)}
