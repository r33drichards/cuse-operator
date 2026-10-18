"""Authenticated, retained Pi Durable control-plane reconciliation.

No Cloudflare administrative API, deployment or credential creation is involved.
An operator-selected HTTPS control endpoint and an existing mounted bearer file
are required. HTTP I/O is injected for tests; redirects and ambient proxies are
never used. Errors contain neither response bodies nor credentials.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
import stat
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

import aiohttp

from .providers import (
    DesiredPlan, Observation, ProviderNotConfigured, ResourceIdentity,
    RetryableProviderError, TerminalProviderError, UncertainOutcome,
)


@dataclass(frozen=True, slots=True)
class DurableRuntimeSettings:
    endpoint: str
    token_file: str

    def validate(self):
        try:
            url = urlsplit(self.endpoint)
            if (url.scheme != "https" or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment
                    or any(c.isspace() or ord(c) < 32 for c in self.endpoint)
                    or url.port == 0 or "\\" in self.endpoint):
                raise ValueError()
            path = Path(self.token_file)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError()
        except (ValueError, TypeError):
            raise TerminalProviderError("Invalid durable runtime configuration.") from None


class AioHttpControlTransport:
    """Small bounded JSON transport, with no session/token retained in repr."""
    async def request(self, method, url, *, token_file, body=None):
        fd = None
        try:
            fd = os.open(token_file, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError()
            with os.fdopen(fd, "rb") as file:
                fd = None
                raw = file.read(8193)
            token = raw.rstrip(b"\r\n")
            if (not 32 <= len(token) <= 8192 or len(raw) > 8192
                    or any(c < 33 or c > 126 for c in token)):
                raise ValueError()
            headers = {"Authorization": "Bearer " + token.decode("ascii"), "Accept": "application/json"}
        except (OSError, ValueError, TypeError):
            raise ProviderNotConfigured("Durable runtime credential file is unavailable or invalid.") from None
        finally:
            if fd is not None:
                os.close(fd)
        try:
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.request(method, url, headers=headers, json=body, allow_redirects=False) as response:
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(8192):
                        size += len(chunk)
                        if size > 65536:
                            raise ValueError()
                        chunks.append(chunk)
                    raw_body = b"".join(chunks)
                    if not 200 <= response.status < 300:
                        return response.status, None
                    return response.status, json.loads(raw_body)
        except (aiohttp.ClientError, OSError, TimeoutError):
            error = UncertainOutcome if method == "PUT" else RetryableProviderError
            raise error("Durable runtime request did not complete reliably.") from None
        except (ValueError, UnicodeError):
            raise RetryableProviderError("Invalid durable runtime response.") from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class DurableRuntimeProvider:
    def __init__(self, settings: DurableRuntimeSettings, *, transport=None):
        settings.validate()
        self.settings = settings
        self.transport = transport if transport is not None else AioHttpControlTransport()
        self.plans = {}

    def bind_plan(self, desired: DesiredPlan):
        ident = desired.identity
        if ident.kind not in {"PiHarness", "Session"}:
            raise ProviderNotConfigured()
        values = [ident.kind, ident.namespace, ident.name, ident.uid]
        if (any(type(v) is not str or not v or any(ord(c) < 32 for c in v) for v in values)
                or desired.revision != _digest(desired.intent)
                or desired.idempotency_key != _digest(values + [desired.revision])):
            raise TerminalProviderError("Invalid durable runtime desired plan.")
        self.plans[ident] = deepcopy(desired)

    def _url(self, identity):
        if identity not in self.plans:
            raise ProviderNotConfigured("Bind a durable runtime plan before observing.")
        parts = [identity.kind, identity.namespace, identity.name, identity.uid]
        return self.settings.endpoint.rstrip("/") + "/v1/resources/" + "/".join(quote(p, safe="") for p in parts)

    async def _request(self, method, identity, body=None):
        status, response = await self.transport.request(method, self._url(identity),
            token_file=self.settings.token_file, body=body)
        if status == 404 and method == "GET":
            return Observation()
        if status in {400, 401, 403, 405, 422}:
            raise TerminalProviderError("Durable runtime rejected the request.")
        if status in {404, 409, 429, 503}:
            raise RetryableProviderError("Durable runtime state requires retry.")
        if not 200 <= status < 300:
            error = UncertainOutcome if method == "PUT" else RetryableProviderError
            raise error("Durable runtime request did not complete reliably.")
        if (type(response) is not dict or response.get("exists") is not True
                or type(response.get("ready")) is not bool
                or type(response.get("revision")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", response["revision"]) is None):
            raise RetryableProviderError("Invalid durable runtime observation.")
        return Observation(True, response["revision"], response["ready"])

    async def observe(self, identity: ResourceIdentity):
        return await self._request("GET", identity)

    async def ensure(self, desired: DesiredPlan, *, idempotency_key: str):
        if idempotency_key != desired.idempotency_key:
            raise TerminalProviderError("Durable runtime idempotency key mismatch.")
        self.bind_plan(desired)
        observed = await self.observe(desired.identity)
        if observed.exists and observed.revision == desired.revision:
            return
        ident = desired.identity
        await self._request("PUT", ident, {
            "identity": {"kind": ident.kind, "namespace": ident.namespace, "name": ident.name, "uid": ident.uid},
            "intent": deepcopy(desired.intent), "revision": desired.revision,
            "idempotency_key": desired.idempotency_key,
            "expected_revision": observed.revision if observed.exists else None,
        })
