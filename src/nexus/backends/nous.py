"""Nous Portal adapter. OpenAI-compatible /v1 pass-through. Key never logged.

Reads the OAuth access token from the local key file. ``./nexus start``
and the in-process refresh in ``nexus.nous_token`` keep that file live;
the token expires every 60 minutes.
"""

from __future__ import annotations

import httpx

from nexus.backends.openai_compat import OpenAICompatibleBackend
from nexus.config import door_origin, nous_base, nous_key_path
from nexus.keys import key_source, load_key

EXTRA_MODELS = (
    "meituan/longcat-2.5-preview:free",
    "meituan/longcat-2.0:free",
    "poolside/laguna-xs-2.1:free",
    "stepfun/step-3.7-flash:free",
    "upstage/solar-pro4:free",
)


def load_nous_key() -> str | None:
    return load_key(env_var="NOUS_API_KEY", path=nous_key_path())


def nous_key_source() -> str | None:
    return key_source(env_var="NOUS_API_KEY", path=nous_key_path())


class NousBackend(OpenAICompatibleBackend):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            backend_id="nous",
            role="primary",
            label="Nous Portal",
            owned_by="nous",
            env_var="NOUS_API_KEY",
            key_path=nous_key_path,
            base_url=nous_base,
            fallback_models=EXTRA_MODELS,
            extra_headers={
                "HTTP-Referer": door_origin(),
                "X-Title": "nexus",
            },
            transport=transport,
        )
