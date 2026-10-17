"""Canonical domain objects. Constructors intentionally perform no I/O or validation."""
from dataclasses import dataclass
from typing import NewType

UserId = NewType("UserId", str)
SessionId = NewType("SessionId", str)


@dataclass(frozen=True, slots=True)
class IRCServer:
    id: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class ChannelRef:
    server_id: str
    name: str


@dataclass(frozen=True, slots=True)
class WeeChat:
    id: str
    server_id: str
    nick: str
    mcp_endpoint: str


@dataclass(frozen=True, slots=True)
class Connector:
    id: str
    server_id: str
    nick: str
    mcp_endpoint: str


@dataclass(frozen=True, slots=True)
class MCPClient:
    id: str
    endpoint: str
    connectors: tuple[Connector, ...] = ()


@dataclass(frozen=True, slots=True)
class Session:
    id: SessionId
    harness_id: str
    channel: ChannelRef
    context_ref: str
    share_url: str
    mcp: MCPClient


@dataclass(frozen=True, slots=True)
class PiHarness:
    id: str
    user_id: UserId
    weechat_id: str
    sessions: tuple[Session, ...] = ()


@dataclass(frozen=True, slots=True)
class Bot:
    id: str
    weechat: WeeChat
    harness: PiHarness


@dataclass(frozen=True, slots=True)
class Architecture:
    irc: IRCServer
    bots: tuple[Bot, ...] = ()
