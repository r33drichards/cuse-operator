"""Validation-only Kopf adapter: no provisioning, network probes or deletion.

Run namespace-bound and standalone (single replica). Framework progress and
change detection use metadata annotations, while application output only
patches the status subresource. A successful handler returns None deliberately:
Kopf must not add a per-handler result field outside the typed status schema.
"""
import asyncio
from copy import deepcopy
import logging
from collections.abc import Mapping
from typing import Any

import kopf

from .codec import architecture_from_dict
from .schema import GROUP, PLURAL, VERSION
from .resources import PLURALS, validate_resource
from .validation import ValidationError


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


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL, field="spec")
def validate(spec: kopf.Spec, meta: kopf.Meta, patch: kopf.Patch, **_: Any) -> None:
    patch.status.update(reconcile(spec, generation=meta.get("generation", 0)))


# Independent resources use raw watches, which do not write progress annotations
# or install finalizers. Their validation reads fresh namespace-local inventories.


class DependencyValidator:
    """One-replica, eventually consistent reference validation, never provisioning.

    Lists across kinds are not transactional. Events and periodic full refreshes
    converge after concurrent edits; resourceVersion/UID prevent stale writes to
    changed or recreated targets. A namespace lock serializes this process's writes.
    """

    def __init__(self, api):
        self.api = api
        self.locks: dict[str, asyncio.Lock] = {}
        self.seen: dict[tuple[str, str, str], Any] = {}

    async def event(self, kind, event):
        body = event["object"]
        meta = body.get("metadata", {})
        namespace, name = meta.get("namespace"), meta.get("name")
        if not namespace or not name:
            return
        self.locks.setdefault(namespace, asyncio.Lock())
        key = namespace, kind, name
        fingerprint = deepcopy((meta.get("uid"), meta.get("generation"),
                                meta.get("deletionTimestamp"), body.get("spec")))
        if event.get("type") == "DELETED":
            self.seen.pop(key, None)
        elif self.seen.get(key) == fingerprint:
            return  # Ignore status-only writes, including our own patches.
        else:
            self.seen[key] = fingerprint
        await self.refresh(namespace)

    async def refresh(self, namespace):
        lock = self.locks.setdefault(namespace, asyncio.Lock())
        async with lock:
            inventory = {}
            for kind, plural in PLURALS.items():
                response = await self.api.list_namespaced_custom_object(
                    GROUP, VERSION, namespace, plural, _request_timeout=30,
                )
                inventory[kind] = {
                    body["metadata"]["name"]: body
                    for body in response["items"]
                    if not body.get("metadata", {}).get("deletionTimestamp")
                }
            flat_inventory = [body for objects in inventory.values() for body in objects.values()]
            for kind, objects in inventory.items():
                for name, body in objects.items():
                    meta = body["metadata"]
                    status = dict(validate_resource(kind, body.get("spec"), namespace, flat_inventory))
                    status["observedGeneration"] = meta.get("generation", 0)
                    if body.get("status") == status:
                        continue
                    # Both identity and optimistic concurrency are enforced by
                    # the apiserver. Never retry a stale patch against a new body.
                    patch = {"metadata": {"uid": meta["uid"],
                                          "resourceVersion": meta["resourceVersion"]},
                             "status": status}
                    try:
                        await self.api.patch_namespaced_custom_object_status(
                            GROUP, VERSION, namespace, PLURALS[kind], name, patch,
                            _content_type="application/merge-patch+json", _request_timeout=30,
                        )
                    except Exception as exc:
                        if getattr(exc, "status", None) not in (404, 409):
                            raise
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


@kopf.on.startup()
async def start_dependencies(memo: kopf.Memo, **_: Any) -> None:
    from kubernetes_asyncio import client, config
    from kubernetes_asyncio.config.config_exception import ConfigException

    try:
        config.load_incluster_config()
    except ConfigException:
        await config.load_kube_config()
    memo.dependency_client = client.ApiClient()
    memo.dependency_validator = DependencyValidator(client.CustomObjectsApi(memo.dependency_client))
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

    kopf.on.event(GROUP, VERSION, plural, id=f"validate-{plural}")(watch)
    return watch


DEPENDENCY_WATCHES = {
    kind: _register_dependency_watch(kind, plural) for kind, plural in PLURALS.items()
}
