"""Pure structural and semantic checks for canonical domain objects.

URL policy: endpoint and share_url values are absolute, hierarchical URLs with
an authority and hostname. No scheme allowlist, network reachability, DNS,
production-only HTTPS, or default port requirement is imposed. URL userinfo,
control/whitespace characters, malformed ports, and credential-bearing query or fragment
parameters are forbidden. context_ref may instead be an opaque storage key or
path; URI-shaped storage references receive the same credential checks (without
requiring a hostname, so file:/// and object-store references work). Credentials
must be supplied outside these models. Arbitrary opaque secrets cannot be
recognized automatically; callers must never put them in paths or opaque keys.

Identity scopes: bot, WeeChat and harness IDs are unique architecture-wide;
session IDs are unique within their harness; connector IDs are unique within
an MCP client. User IDs may intentionally be shared across harnesses. A session
owns its MCP client, so MCP client IDs need not be globally unique.
"""
from dataclasses import dataclass, fields, is_dataclass
import re
from typing import get_args, get_origin, get_type_hints
from urllib.parse import parse_qsl, unquote, urlsplit

from .models import Architecture, Bot, ChannelRef, Connector, IRCServer, MCPClient, PiHarness, Session, WeeChat


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    path: str
    message: str


class ValidationError(ValueError):
    """A stable machine-readable issue and a human-readable exception message."""
    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        self.errors = (ValidationIssue(path, message),)
        super().__init__(f"{path}: {message}")


def _fail(path, message):
    raise ValidationError(path, message)


def _structure(expected, value, path):
    if hasattr(expected, "__supertype__"):
        expected = expected.__supertype__
    if expected is str:
        if type(value) is not str:
            _fail(path, "expected string")
    elif get_origin(expected) is tuple:
        if type(value) is not tuple:
            _fail(path, "expected tuple")
        item_type, _ = get_args(expected)
        for i, item in enumerate(value):
            _structure(item_type, item, f"{path}[{i}]")
    elif is_dataclass(expected):
        if type(value) is not expected:
            _fail(path, f"expected {expected.__name__}")
        hints = get_type_hints(expected)
        for field in fields(expected):
            _structure(hints[field.name], getattr(value, field.name), f"{path}.{field.name}")
    else:
        raise TypeError(f"unsupported domain field type: {expected!r}")


_CREDENTIAL_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "token", "accesstoken", "refreshtoken",
    "apikey", "authorization", "credential", "credentials", "clientsecret",
    "signature", "sig", "xamzsignature", "xamzcredential", "xgoogsignature", "xgoogcredential",
})


def _url(value, path, *, storage=False):
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(path, "URL or storage reference must not contain whitespace or controls")
    if "\\" in value:
        _fail(path, "URL or storage reference must not contain backslashes")
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            _fail(path, "credentials in URL userinfo are forbidden")
        # Accessing these properties validates bracketed hosts and port syntax/range.
        host = parsed.hostname
        _ = parsed.port
        if not storage and (not parsed.scheme or not parsed.netloc or not host):
            _fail(path, "expected absolute URL with hostname")
        if parsed.netloc and not host:
            _fail(path, "URL authority must have a hostname")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True) + parse_qsl(parsed.fragment, keep_blank_values=True):
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _CREDENTIAL_KEYS:
                _fail(path, "credential-bearing query parameters are forbidden")
        # Percent encoding must not hide userinfo in authority.
        if "@" in unquote(parsed.netloc):
            _fail(path, "encoded credentials in URL authority are forbidden")
    except ValueError as error:
        if isinstance(error, ValidationError):
            raise
        _fail(path, "malformed URL or storage reference")


def _unique(items, path):
    seen = set()
    for i, item in enumerate(items):
        if item.id in seen:
            _fail(f"{path}[{i}].id", "duplicate ID within scope")
        seen.add(item.id)


def _semantic(obj, path):
    for field in fields(obj):
        value = getattr(obj, field.name)
        child = f"{path}.{field.name}"
        if type(value) is str:
            if not value or not value.strip():
                _fail(child, "must not be empty or whitespace-only")
            if field.name in {"id", "server_id", "harness_id", "weechat_id", "user_id"}:
                if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    _fail(child, "ID must not contain surrounding whitespace or controls")
            if field.name in {"endpoint", "mcp_endpoint", "share_url", "context_ref"}:
                _url(value, child, storage=field.name == "context_ref")
        elif is_dataclass(value):
            _semantic(value, child)
        elif type(value) is tuple:
            _unique(value, child)
            for i, item in enumerate(value):
                _semantic(item, f"{child}[{i}]")
    if isinstance(obj, ChannelRef):
        if obj.name[0] not in "#&+!" or len(obj.name) == 1:
            _fail(f"{path}.name", "IRC channel must have a standard prefix and a name")
        if any(c.isspace() or c in ",:\x07\x00" or ord(c) < 32 or ord(c) == 127 for c in obj.name):
            _fail(f"{path}.name", "IRC channel contains a forbidden character")
    if isinstance(obj, (WeeChat, Connector)):
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in obj.nick):
            _fail(f"{path}.nick", "nick must not contain whitespace or controls")
    if isinstance(obj, Session):
        for i, connector in enumerate(obj.mcp.connectors):
            if connector.server_id != obj.channel.server_id:
                _fail(f"{path}.mcp.connectors[{i}].server_id", "must match the session channel server")
    if isinstance(obj, PiHarness):
        for i, session in enumerate(obj.sessions):
            if session.harness_id != obj.id:
                _fail(f"{path}.sessions[{i}].harness_id", "must refer to the containing harness")
    if isinstance(obj, Bot):
        if obj.harness.weechat_id != obj.weechat.id:
            _fail(f"{path}.harness.weechat_id", "must refer to this bot's WeeChat")
        for i, session in enumerate(obj.harness.sessions):
            if session.channel.server_id != obj.weechat.server_id:
                _fail(f"{path}.harness.sessions[{i}].channel.server_id", "must match this bot's WeeChat server")
    if isinstance(obj, Architecture):
        seen_weechats, seen_harnesses = set(), set()
        for i, bot in enumerate(obj.bots):
            base = f"{path}.bots[{i}]"
            for entity, seen in (("weechat", seen_weechats), ("harness", seen_harnesses)):
                identifier = getattr(bot, entity).id
                if identifier in seen:
                    _fail(f"{base}.{entity}.id", "duplicate ID within architecture")
                seen.add(identifier)
            if bot.weechat.server_id != obj.irc.id:
                _fail(f"{base}.weechat.server_id", "unknown IRC server")
            for j, session in enumerate(bot.harness.sessions):
                for k, connector in enumerate(session.mcp.connectors):
                    if connector.server_id != obj.irc.id:
                        _fail(f"{base}.harness.sessions[{j}].mcp.connectors[{k}].server_id", "unknown IRC server")


def validate_domain(obj):
    """Validate types, local semantics and all relationships present in obj."""
    if type(obj) not in {Architecture, Bot, ChannelRef, Connector, IRCServer, MCPClient, PiHarness, Session, WeeChat}:
        raise TypeError("expected canonical domain dataclass")
    _structure(type(obj), obj, "$")
    _semantic(obj, "$")


def validate_architecture(architecture: Architecture) -> None:
    """Validate a complete architecture; raise ValidationError on the first issue."""
    _structure(Architecture, architecture, "$")
    _semantic(architecture, "$")
