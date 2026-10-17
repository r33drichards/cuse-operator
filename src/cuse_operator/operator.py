"""Validation-only Kopf adapter: no provisioning, network probes or deletion.

Run namespace-bound and standalone (single replica). Framework progress and
change detection use metadata annotations, while application output only
patches the status subresource. A successful handler returns None deliberately:
Kopf must not add a per-handler result field outside the typed status schema.
"""
from collections.abc import Mapping
from typing import Any

import kopf

from .codec import architecture_from_dict
from .schema import GROUP, PLURAL, VERSION
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
