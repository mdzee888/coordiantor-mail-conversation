"""
mailer.py - one send/reply API that dispatches to the provider a mail arrived on.

Each provider module exposes:
    send_mail(*, to, subject, text, cc=None, bcc=None) -> {"message_id", "thread_id", ...}
    reply_mail(original_norm_msg, *, text, subject=None) -> {"message_id", "thread_id", ...}

`original_norm_msg` is the normalized dict the provider's _normalize() produced.
"""
from __future__ import annotations

from typing import Optional


def _provider_module(provider: str):
    if provider == "agentmail":
        from providers import agentmail as mod
    elif provider == "gmail":
        from providers import gmail as mod
    elif provider == "m365":
        from providers import m365 as mod
    else:
        raise ValueError(f"unknown provider: {provider!r}")
    return mod


def send(provider: str, *, to: str, subject: str, text: str,
         cc: Optional[list] = None, bcc: Optional[list] = None) -> dict:
    return _provider_module(provider).send_mail(
        to=to, subject=subject, text=text, cc=cc or [], bcc=bcc or [],
    )


def reply(provider: str, original: dict, *, text: str, subject: Optional[str] = None,
          cc: Optional[list] = None, bcc: Optional[list] = None) -> dict:
    return _provider_module(provider).reply_mail(
        original, text=text, subject=subject, cc=cc or [], bcc=bcc or [],
    )
