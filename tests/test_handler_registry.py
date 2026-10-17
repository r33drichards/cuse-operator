"""Exercise Kopf's registered handler selection without connecting to a cluster."""
import logging

import kopf
import pytest
from kopf._cogs.structs.bodies import Body
from kopf._cogs.structs.diffs import diff
from kopf._cogs.structs.references import Resource
from kopf._core.intents.causes import ChangingCause, Reason

from cuse_operator.operator import reconcile


SPEC = {"irc": {"id": "irc", "endpoint": "ircs://irc.example.test"}}


@pytest.mark.parametrize(
    "reason,initial,old,new,expected",
    [
        (Reason.CREATE, False, None, {"spec": SPEC}, []),
        (Reason.RESUME, True, {"spec": SPEC}, {"spec": SPEC}, []),
        (
            Reason.UPDATE, False,
            {"spec": {"irc": {"id": "old", "endpoint": "ircs://irc.example.test"}}},
            {"spec": SPEC}, [],
        ),
        (
            Reason.UPDATE, False,
            {"spec": SPEC, "status": {"phase": "Invalid"}},
            {"spec": SPEC, "status": {"phase": "Valid"}}, [],
        ),
        (
            Reason.UPDATE, False,
            {"spec": SPEC, "metadata": {"labels": {"example": "before"}}},
            {"spec": SPEC, "metadata": {"labels": {"example": "after"}}}, [],
        ),
    ],
    ids=["create", "resume", "spec-update", "status-only", "metadata-only"],
)
def test_registered_handler_selection(reason, initial, old, new, expected):
    cause = ChangingCause(
        logger=logging.getLogger(__name__), indices={}, memo={},
        resource=Resource(
            "cuse.io", "v1alpha1", "architectures", kind="Architecture", namespaced=True,
        ),
        patch=kopf.Patch(), body=Body(new), initial=initial, reason=reason,
        old=old, new=new, diff=diff(old, new),
    )
    handlers = kopf.get_default_registry()._changing.get_handlers(cause)
    assert [str(handler.id) for handler in handlers] == expected
    # Architecture has only the raw event controller, never a competing changer.
    assert not handlers


def test_actual_kopf_spec_mapping_is_decoded():
    body = Body({"spec": SPEC})
    assert reconcile(body.spec)["phase"] == "Valid"
