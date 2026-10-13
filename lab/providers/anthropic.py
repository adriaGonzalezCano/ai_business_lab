"""Anthropic Messages API adapter (stdlib HTTP, no SDK dependency).

Requires ANTHROPIC_API_KEY. The key is only ever read from the environment on the server and is never
returned by any API route. Pricing is NOT known here: it comes from the owner's `model_pricing` setting.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from ..util import redact
from .base import ProviderError, ProviderRequest, ProviderResult, ProviderUnavailable

API_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"
    synthetic = False

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com", timeout: float = 60.0,
                 opener: Callable[..., object] | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "Set the ANTHROPIC_API_KEY environment variable on the server, then restart."
        return True, "ok"

    def complete(self, req: ProviderRequest) -> ProviderResult:
        ok, why = self.available()
        if not ok:
            raise ProviderUnavailable(why)
        body = json.dumps({
            "model": req.model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.prompt}],
        }).encode()
        http_req = urllib.request.Request(
            f"{self.base_url}/v1/messages", data=body, method="POST",
            headers={"content-type": "application/json", "x-api-key": self.api_key,
                     "anthropic-version": API_VERSION},
        )
        try:
            with self._open(http_req, timeout=self.timeout) as resp:  # type: ignore[misc]
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = redact(e.read().decode("utf-8", "replace")[:300]) if hasattr(e, "read") else ""
            raise ProviderError(f"anthropic HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise ProviderError(f"anthropic request failed: {redact(str(e))}") from None
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        usage = payload.get("usage") or {}
        if "input_tokens" not in usage or "output_tokens" not in usage:
            raise ProviderError("anthropic response did not report token usage")
        return ProviderResult(text=text, input_tokens=int(usage["input_tokens"]),
                              output_tokens=int(usage["output_tokens"]))
