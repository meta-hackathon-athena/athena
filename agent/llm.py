"""Optional LLM provider. The agent works without one (rules only).

The standalone CLI shares the integrated runtime's Meta credential:
  VIXY_LLM_MODEL     model name; unset = no LLM          (legacy name: VEYRA_LLM_MODEL)
  MODEL_API_KEY     shared with Dreamer and M2 reasoning
  Endpoint: https://api.meta.ai/v1/chat/completions

OpenAICompatibleLLM remains injectable for explicitly configured local/test providers.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Protocol


class LLM(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 200) -> str: ...


class OpenAICompatibleLLM:
    def __init__(self, model: str, base_url: str, api_key: str, timeout_s: float = 4.0):
        self.model, self.base_url, self.api_key, self.timeout_s = model, base_url.rstrip("/"), api_key, timeout_s

    def complete(self, system: str, user: str, max_tokens: int = 200) -> str:
        body = json.dumps({
            "model": self.model, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]


def from_env() -> LLM | None:
    from prod_dreamer_model.config import _load_env_files, is_contributor_model
    _load_env_files()
    model = os.getenv("VIXY_LLM_MODEL") or os.getenv("VEYRA_LLM_MODEL")
    key = os.getenv("MODEL_API_KEY")
    if not model or not key:
        return None
    if is_contributor_model(model):
        raise ValueError("contributor models are forbidden for wearable speech")
    return OpenAICompatibleLLM(model, "https://api.meta.ai/v1", key)
