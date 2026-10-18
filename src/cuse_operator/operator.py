"""Namespace-local Kopf watches dispatch to eight dedicated controllers.

The default provider is disabled. Raw watches never install finalizers or write
progress annotations; provider deletion retains external resources by policy.
"""
import asyncio
from copy import deepcopy
import logging
from collections.abc import Mapping
from typing import Any

import kopf

from .codec import architecture_from_dict
from .schema import GROUP, PLURAL, VERSION
from .resources import PLURALS
from .controllers import CONTROLLERS
from .providers import DisabledProvider

from .validation import ValidationError

ALL_PLURALS = {**PLURALS, "Architecture": PLURAL}


def reconcile(spec: Any, generation: int = 0) -> dict[str, Any]:
    """Pure, idempotent dry reconciliation. Never echo input or exception text.

Validity means configuration validation only, not connectivity or readiness.
An invalid spec is a completed validation result rather than a retryable error.
"""
    try:
        architecture_from_dict(dict(spec) if isinstance(spec, Mapping) else spec)
    except ValidationError:
        return {
            "phase": "Invalid",
            "observedGeneration": generation,
            "reason": "ValidationFailed",
            "message": "Configuration validation failed; validate the source document locally for details.",
        }
    return {
        "phase": "Valid",
        "observedGeneration": generation,
        "reason": "Validated",
        "message": "Configuration is valid; no resources have been provisioned or probed.",
    }


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix=GROUP)
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(prefix=GROUP)
    settings.posting.enabled = False
    # Namespace is explicitly selected at startup; no namespace/CRD watch RBAC.
    settings.scanning.disabled = True


def validate(spec: kopf.Spec, meta: kopf.Meta, patch: kopf.Patch, **_: Any) -> None:
    """Undecorated compatibility helper; active Architecture uses its controller."""
    patch.status.update(reconcile(spec, generation=meta.get("generation", 0)))


# Independent resources use raw watches, which do not write progress annotations
# or install finalizers. Their validation reads fresh namespace-local inventories.


class ControllerManager:
    """Serialize namespace refreshes; retry fresh reads on periodic resync.

    Lists are eventually consistent. UID/resourceVersion guard status writes.
    All provider calls are bounded and the default cannot provision anything.
    """

    def __init__(self, api, provider=None, controllers=None, timeout=30):
        self.api = api
        self.provider = provider if provider is not None else DisabledProvider()
        self.controllers = CONTROLLERS if controllers is None else controllers
        self.timeout = timeout
        self.locks: dict[str, asyncio.Lock] = {}
        self.seen: dict[tuple[str, str, str], Any] = {}
        self.pending_deletes = {}

    async def event(self, kind, event):
        body = event["object"]
        meta = body.get("metadata", {})
        namespace, name = meta.get("namespace"), meta.get("name")
        if not namespace or not name or kind not in self.controllers:
            return
        lock = self.locks.setdefault(namespace, asyncio.Lock())
        key = namespace, kind, name
        fingerprint = deepcopy((meta.get("uid"), meta.get("generation"),
                                meta.get("deletionTimestamp"), body.get("spec")))
        async with lock:
            if event.get("type") == "DELETED":
                self.seen.pop(key, None)
                self.pending_deletes[(*key, meta.get("uid"))] = deepcopy(body)
            elif self.seen.get(key) == fingerprint:
                return  # Status-only changes do not recursively trigger writes.
            else:
                self.seen[key] = fingerprint
            await self._refresh(namespace)

    async def refresh(self, namespace):
        async with self.locks.setdefault(namespace, asyncio.Lock()):
            await self._refresh(namespace)

    async def _delete(self, kind, body):
        try:
            async with asyncio.timeout(self.timeout):
                await self.controllers[kind].on_delete(body, self.provider)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning(
                "Deletion retention callback failed; retrying on periodic resync.")
            return False

    async def _refresh(self, namespace):
        for key, body in list(self.pending_deletes.items()):
            if key[0] == namespace and await self._delete(key[1], body):
                del self.pending_deletes[key]
        inventory = {}
        deleting = []
        for kind, plural in ALL_PLURALS.items():
            async with asyncio.timeout(self.timeout):
                response = await self.api.list_namespaced_custom_object(
                    GROUP, VERSION, namespace, plural, _request_timeout=30,
                )
            inventory[kind] = {}
            for body in response["items"]:
                if body.get("metadata", {}).get("deletionTimestamp"):
                    deleting.append((kind, body))
                else:
                    inventory[kind][body["metadata"]["name"]] = body
        for kind, body in deleting:
            await self._delete(kind, body)
        flat_inventory = [body for objects in inventory.values() for body in objects.values()]
        # ALL_PLURALS is dependency ordered, with the aggregate last.
        for kind, objects in inventory.items():
            for name, body in objects.items():
                meta = body["metadata"]
                try:
                    async with asyncio.timeout(self.timeout):
                        status = await self.controllers[kind].reconcile(
                            body, flat_inventory, self.provider)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # One slow or broken controller cannot starve independent
                    # objects or leave a previously-ready object falsely ready.
                    status = {
                        "phase": "Invalid", "reason": "ValidationFailed",
                        "message": "Controller reconciliation could not complete; retry is scheduled.",
                        "observedGeneration": meta.get("generation", 0),
                        "ready": False,
                        "reconciliationReason": ("RetryableProviderError" if isinstance(exc, TimeoutError)
                                                 else "ProviderError"),
                        "desiredRevision": "", "observedRevision": "", "lastAction": "None",
                    }
                if body.get("status") == status:
                    continue
                patch = {"metadata": {"uid": meta["uid"],
                                      "resourceVersion": meta["resourceVersion"]},
                         "status": status}
                try:
                    async with asyncio.timeout(self.timeout):
                        await self.api.patch_namespaced_custom_object_status(
                            GROUP, VERSION, namespace, ALL_PLURALS[kind], name, patch,
                            _content_type="application/merge-patch+json", _request_timeout=30,
                        )
                    body["status"] = status
                except Exception as exc:
                    if getattr(exc, "status", None) not in (404, 409):
                        raise
                    # Do not expose unpersisted readiness to dependent controllers.
                    # Periodic fresh reads retry conflicts/deletions safely.

    async def resync(self, interval=30):
        while True:
            await asyncio.sleep(interval)
            for namespace in tuple(self.locks):
                try:
                    await self.refresh(namespace)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Do not log bodies or credentials from exception messages.
                    logging.getLogger(__name__).warning(
                        "Dependency validation refresh failed; retrying on next resync.")


DependencyValidator = ControllerManager  # Backward-compatible import.


@kopf.on.startup()
async def start_dependencies(memo: kopf.Memo, **_: Any) -> None:
    from kubernetes_asyncio import client, config
    from kubernetes_asyncio.config.config_exception import ConfigException

    try:
        config.load_incluster_config()
    except ConfigException:
        await config.load_kube_config()
    from .runtime import build_provider, load_provider_config

    provider_config = load_provider_config()
    memo.dependency_client = client.ApiClient()
    provider = build_provider(provider_config,
        apps=client.AppsV1Api(memo.dependency_client), core=client.CoreV1Api(memo.dependency_client))
    memo.dependency_validator = DependencyValidator(client.CustomObjectsApi(memo.dependency_client), provider=provider)
    memo.dependency_resync = asyncio.create_task(memo.dependency_validator.resync())


@kopf.on.cleanup()
async def stop_dependencies(memo: kopf.Memo, **_: Any) -> None:
    task = memo.get("dependency_resync")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    api_client = memo.get("dependency_client")
    if api_client is not None:
        await api_client.close()


def _register_dependency_watch(kind, plural):
    async def watch(event: kopf.RawEvent, memo: kopf.Memo, **_: Any) -> None:
        try:
            await memo.dependency_validator.event(kind, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Kopf otherwise logs raw API exceptions, which can contain bodies.
            logging.getLogger(__name__).warning(
                "Dependency event validation failed; retrying on periodic resync.")

    kopf.on.event(GROUP, VERSION, plural, id=f"reconcile-{plural}")(watch)
    return watch


DEPENDENCY_WATCHES = {
    kind: _register_dependency_watch(kind, plural) for kind, plural in ALL_PLURALS.items()
}
