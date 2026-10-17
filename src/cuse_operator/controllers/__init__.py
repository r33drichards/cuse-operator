"""Dedicated kind controllers; registry order is dependency-first."""
from .ircserver import controller as ircserver
from .weechat import controller as weechat
from .connector import controller as connector
from .mcpclient import controller as mcpclient
from .piharness import controller as piharness
from .session import controller as session
from .bot import controller as bot
from .architecture import controller as architecture

CONTROLLERS = {controller.kind: controller for controller in
               (ircserver, weechat, connector, mcpclient, piharness, session, bot, architecture)}
