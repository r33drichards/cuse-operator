"""Pure, namespaced Kubernetes reference contracts.

References address Kubernetes metadata names, optionally pinned by UID. Domain
IDs are a separate identity space, unique per kind and namespace. This module
performs no Kubernetes calls and never returns submitted data in status text.
"""
from __future__ import annotations

import re
from typing import Iterable, Literal, TypedDict

from .models import Bot, ChannelRef, Connector, IRCServer, MCPClient, PiHarness, Session, WeeChat
from .validation import ValidationError, validate_domain


class ResourceStatus(TypedDict):
    phase: Literal["Valid", "Invalid", "Blocked"]
    reason: Literal["Validated", "ValidationFailed", "MissingDependency", "ReferenceMismatch", "DuplicateIdentity"]
    message: str


def _object(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


def _string():
    return {"type": "string", "minLength": 1}


def _ref():
    return _object({"name": {"type": "string", "minLength": 1, "maxLength": 253,
                              "pattern": r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"},
                    "uid": _string()}, ["name"])


RESOURCE_SPECS = {
    "IRCServer": _object({"id": _string(), "endpoint": _string()}),
    "WeeChat": _object({"id": _string(), "serverRef": _ref(), "nick": _string(), "mcp_endpoint": _string()}),
    "Connector": _object({"id": _string(), "serverRef": _ref(), "nick": _string(), "mcp_endpoint": _string()}),
    "MCPClient": _object({"id": _string(), "endpoint": _string(), "connectorRefs": {
        "type": "array", "items": _ref(), "default": []}}, ["id", "endpoint"]),
    "PiHarness": _object({"id": _string(), "user_id": _string(), "weechatRef": _ref()}),
    "Session": _object({"id": _string(), "harnessRef": _ref(), "channel": _object({
        "serverRef": _ref(), "name": _string()}), "context_ref": _string(),
        "share_url": _string(), "mcpClientRef": _ref()}),
    "Bot": _object({"id": _string(), "weechatRef": _ref(), "harnessRef": _ref()}),
}
PLURALS = {"IRCServer": "ircservers", "WeeChat": "weechats", "Connector": "connectors",
           "MCPClient": "mcpclients", "PiHarness": "piharnesses", "Session": "sessions", "Bot": "bots"}
_MESSAGES = {
    "Validated": "Resource and dependencies are valid.",
    "ValidationFailed": "Resource or dependency specification is invalid.",
    "MissingDependency": "A required dependency is missing or deleting.",
    "ReferenceMismatch": "A reference identity or relationship does not match.",
    "DuplicateIdentity": "A domain identity is duplicated in this namespace.",
}


class ResourceSpecError(ValueError):
    """A deliberately generic, safe-to-display contract error."""


class _Problem(Exception):
    def __init__(self, reason):
        self.reason = reason


def _shape(value, schema):
    kind = schema["type"]
    if kind == "object":
        if type(value) is not dict:
            raise ResourceSpecError("Invalid resource specification.")
        properties = schema["properties"]
        if set(value) - set(properties) or not set(schema["required"]).issubset(value):
            raise ResourceSpecError("Invalid resource specification.")
        for key, item in value.items():
            _shape(item, properties[key])
    elif kind == "array":
        if type(value) is not list:
            raise ResourceSpecError("Invalid resource specification.")
        for item in value:
            _shape(item, schema["items"])
    elif (type(value) is not str or not value.strip()
          or len(value) > schema.get("maxLength", len(value))
          or ("pattern" in schema and re.fullmatch(schema["pattern"], value) is None)):
        raise ResourceSpecError("Invalid resource specification.")


def _local_domain(kind, spec):
    """Use canonical validators for all scalar fields before looking up refs."""
    identifier = spec["id"]
    if kind == "IRCServer":
        return IRCServer(identifier, spec["endpoint"])
    if kind in {"WeeChat", "Connector"}:
        cls = WeeChat if kind == "WeeChat" else Connector
        return cls(identifier, "server", spec["nick"], spec["mcp_endpoint"])
    if kind == "MCPClient":
        return MCPClient(identifier, spec["endpoint"])
    if kind == "PiHarness":
        return PiHarness(identifier, spec["user_id"], "weechat")
    if kind == "Session":
        return Session(identifier, "harness", ChannelRef("server", spec["channel"]["name"]),
                       spec["context_ref"], spec["share_url"], MCPClient("client", "https://example.invalid"))
    return Bot(identifier, WeeChat("weechat", "server", "nick", "https://example.invalid"),
               PiHarness("harness", "user", "weechat"))


def validate_resource_spec(kind: str, spec: dict) -> None:
    """Validate only the local contract; errors never contain submitted values."""
    try:
        if kind not in RESOURCE_SPECS:
            raise ResourceSpecError("Invalid resource specification.")
        _shape(spec, RESOURCE_SPECS[kind])
        validate_domain(_local_domain(kind, spec))
    except (ValidationError, KeyError, TypeError, ValueError, RecursionError):
        raise ResourceSpecError("Invalid resource specification.") from None


def validate_resource(kind: str, spec: dict, namespace: str,
                      inventory: Iterable[dict]) -> ResourceStatus:
    """Validate against a complete, immutable-in-use namespaced snapshot.

    The inventory must include the current object. Missing snapshot entries are
    treated as missing dependencies. Dependency status fields are never trusted:
    every referenced spec is checked transitively. Names and UID pins resolve
    Kubernetes objects; relationship comparisons use their canonical domain IDs.
    No resource, status or inventory object is modified.
    """
    try:
        validate_resource_spec(kind, spec)
        if type(namespace) is not str or not namespace:
            raise ResourceSpecError("Invalid resource specification.")
        index = {}
        identities = {}
        for body in inventory:
            if type(body) is not dict or type(body.get("metadata")) is not dict:
                continue
            metadata = body["metadata"]
            resource_kind, ns, name = body.get("kind"), metadata.get("namespace"), metadata.get("name")
            if type(resource_kind) is not str or type(ns) is not str or type(name) is not str:
                continue
            key = (resource_kind, ns, name)
            index[key] = body
            body_spec = body.get("spec")
            if type(body_spec) is dict and type(body_spec.get("id")) is str:
                identities.setdefault((resource_kind, ns, body_spec["id"]), set()).add(name)
        active = set()
        cache = {}

        def unique(resource_kind, resource_spec):
            if len(identities.get((resource_kind, namespace, resource_spec["id"]), ())) > 1:
                raise _Problem("DuplicateIdentity")

        def resolve(resource_kind, ref):
            key = (resource_kind, namespace, ref["name"])
            body = index.get(key)
            if body is None or body["metadata"].get("deletionTimestamp"):
                raise _Problem("MissingDependency")
            if "uid" in ref and ref["uid"] != body["metadata"].get("uid"):
                raise _Problem("ReferenceMismatch")
            if key in active or len(active) >= 32:
                raise _Problem("ReferenceMismatch")
            if key not in cache:
                active.add(key)
                try:
                    cache[key] = build(resource_kind, body.get("spec"))
                finally:
                    active.remove(key)
            return cache[key]

        def build(resource_kind, resource_spec):
            validate_resource_spec(resource_kind, resource_spec)
            unique(resource_kind, resource_spec)
            identifier = resource_spec["id"]
            if resource_kind == "IRCServer":
                obj = IRCServer(identifier, resource_spec["endpoint"])
            elif resource_kind in {"WeeChat", "Connector"}:
                server = resolve("IRCServer", resource_spec["serverRef"])
                cls = WeeChat if resource_kind == "WeeChat" else Connector
                obj = cls(identifier, server.id, resource_spec["nick"], resource_spec["mcp_endpoint"])
            elif resource_kind == "MCPClient":
                connectors = tuple(resolve("Connector", ref) for ref in resource_spec.get("connectorRefs", []))
                obj = MCPClient(identifier, resource_spec["endpoint"], connectors)
            elif resource_kind == "PiHarness":
                weechat = resolve("WeeChat", resource_spec["weechatRef"])
                obj = PiHarness(identifier, resource_spec["user_id"], weechat.id)
            elif resource_kind == "Bot":
                weechat = resolve("WeeChat", resource_spec["weechatRef"])
                harness = resolve("PiHarness", resource_spec["harnessRef"])
                if harness.weechat_id != weechat.id:
                    raise _Problem("ReferenceMismatch")
                obj = Bot(identifier, weechat, harness)
            else:  # Session
                harness = resolve("PiHarness", resource_spec["harnessRef"])
                harness_body = index[("PiHarness", namespace, resource_spec["harnessRef"]["name"])]
                weechat = resolve("WeeChat", harness_body["spec"]["weechatRef"])
                server = resolve("IRCServer", resource_spec["channel"]["serverRef"])
                client = resolve("MCPClient", resource_spec["mcpClientRef"])
                if weechat.server_id != server.id or any(c.server_id != server.id for c in client.connectors):
                    raise _Problem("ReferenceMismatch")
                obj = Session(identifier, harness.id, ChannelRef(server.id, resource_spec["channel"]["name"]),
                              resource_spec["context_ref"], resource_spec["share_url"], client)
            validate_domain(obj)
            return obj

        build(kind, spec)
        reason = "Validated"
    except _Problem as error:
        reason = error.reason
    except (ResourceSpecError, ValidationError, KeyError, TypeError, ValueError, RecursionError):
        reason = "ValidationFailed"
    return {"phase": "Valid" if reason == "Validated" else "Blocked" if reason == "MissingDependency" else "Invalid",
            "reason": reason, "message": _MESSAGES[reason]}
