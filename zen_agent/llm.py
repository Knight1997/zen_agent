"""Minimal Ollama chat client built on the Python standard library.

We intentionally avoid third-party HTTP libraries so the project runs with a
clean Python install. The Ollama server is expected to be reachable at
``OLLAMA_HOST`` (default ``http://localhost:11434``).
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = os.environ.get("ZEN_MODEL", "llama3.2:1b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


class LLMError(RuntimeError):
    """Raised when the Ollama backend cannot be reached or returns an error."""


@dataclass
class LLMClient:
    """Thin wrapper around the Ollama ``/api/chat`` endpoint."""

    model: str = DEFAULT_MODEL
    host: str = OLLAMA_HOST
    temperature: float = 0.0
    timeout: float = 120.0
    max_retries: int = 2
    # Captures every request/response pair for trace logging.
    call_log: list[dict[str, Any]] = field(default_factory=list)

    def chat(
        self,
        system: str,
        user: str,
        stop: list[str] | None = None,
        temperature: float | None = None,
    ) -> str:
        """Send a single system+user turn and return the assistant text."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
            },
        }
        if stop:
            body["options"]["stop"] = stop

        data = json.dumps(body).encode("utf-8")

        # Retry transient timeouts/connection errors; a slow first token from a
        # cold model should not crash a whole run.
        last_exc: Exception | None = None
        payload: dict[str, Any] | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.host}/api/chat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except (socket.timeout, TimeoutError) as exc:
                last_exc = exc
                time.sleep(1.0 * (attempt + 1))
                continue
            except urllib.error.URLError as exc:
                # A wrapped timeout is also transient; otherwise fail fast.
                if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    last_exc = exc
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise LLMError(
                    f"Could not reach Ollama at {self.host}: {exc}. "
                    "Is `ollama serve` running and the model pulled?"
                ) from exc
            except json.JSONDecodeError as exc:
                raise LLMError(f"Ollama returned invalid JSON: {exc}") from exc

        if payload is None:
            raise LLMError(
                f"Ollama at {self.host} timed out after {self.max_retries + 1} "
                f"attempts ({self.timeout}s each): {last_exc}"
            )

        content = payload.get("message", {}).get("content", "")
        self.call_log.append(
            {
                "system": system,
                "user": user,
                "stop": stop,
                "response": content,
            }
        )
        return content
