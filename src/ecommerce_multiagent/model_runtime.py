from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ModelRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelAudit:
    model: str
    provider: str
    status: str
    issues: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_ms: float
    attempts: int = 1

    def trace_summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "status": self.status,
            "issue_count": len(self.issues),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
        }


class ModelRuntime(Protocol):
    enabled: bool

    def model_for(self, assigned_model: str) -> str: ...

    def audit(self, agent: str, model: str, payload: dict[str, Any]) -> ModelAudit | None: ...


AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["accept", "review"]},
        "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["status", "issues"],
    "additionalProperties": False,
}


def audit_system_prompt(agent: str) -> str:
    return (
        f"You are {agent} in an Olist support investigation. Inspect only the "
        "provided structured source projection and computed report. Never invent "
        "orders, payments, sellers, timestamps, refunds, or evidence. Return JSON "
        "only: {\"status\":\"accept\" or \"review\",\"issues\":[short strings]}. "
        "Use accept when the report is internally consistent with the source. "
        "Return at most 2 issues, each at most 12 words; do not explain outside JSON."
    )


def _parse_audit_json(content: str) -> tuple[str, list[str]]:
    """Parse a bounded audit object even if a local model adds code fences."""
    candidate = content.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        audit = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        audit = json.loads(candidate[start : end + 1])
    status, issues = audit.get("status"), audit.get("issues", [])
    if status not in {"accept", "review"} or not isinstance(issues, list):
        raise ValueError(f"invalid structured audit: {audit!r}")
    return status, [str(value)[:200] for value in issues[:5]]


class OllamaRuntime:
    """Small Ollama client used for bounded, structured agent audits.

    Models inspect domain packets/reports but cannot directly mutate canonical
    numeric fields. This keeps the actual model calls observable without making
    scored JSON depend on sampling.
    """

    def __init__(self, base_url: str, mode: str = "off", timeout: float = 300.0, model_override: str | None = None) -> None:
        if mode not in {"off", "optional", "required"}:
            raise ValueError(f"Unknown model mode: {mode}")
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.timeout = timeout
        self.model_override = model_override

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def model_for(self, assigned_model: str) -> str:
        return self.model_override or assigned_model

    def audit(self, agent: str, model: str, payload: dict[str, Any]) -> ModelAudit | None:
        if not self.enabled:
            return None
        system = audit_system_prompt(agent)
        body = {
            "model": model,
            "stream": False,
            "format": AUDIT_SCHEMA,
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_predict": 256, "seed": 42, "num_ctx": 4096},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(1, 3):
            request = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                content = result.get("message", {}).get("content", "")
                audit = json.loads(content)
                status = audit.get("status")
                issues = audit.get("issues", [])
                if status not in {"accept", "review"} or not isinstance(issues, list):
                    raise ValueError(f"invalid structured audit: {audit!r}")
                return ModelAudit(
                    model=result.get("model", model),
                    provider="ollama",
                    status=status,
                    issues=[str(value)[:200] for value in issues[:5]],
                    prompt_tokens=result.get("prompt_eval_count"),
                    completion_tokens=result.get("eval_count"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    attempts=attempt,
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
                last_error = exc
        if self.mode == "required":
            raise ModelRuntimeError(f"{agent} could not call {model} after 2 attempts: {last_error}") from last_error
        return ModelAudit(
            model=model,
            provider="ollama",
            status="runtime_error",
            issues=[str(last_error)[:200]],
            prompt_tokens=None,
            completion_tokens=None,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            attempts=2,
        )


class OpenAIRuntime:
    """OpenAI Chat Completions adapter with strict structured outputs."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        mode: str = "required",
        timeout: float = 300.0,
    ) -> None:
        if mode not in {"off", "optional", "required"}:
            raise ValueError(f"Unknown model mode: {mode}")
        if mode != "off" and not api_key:
            raise ModelRuntimeError("OPENAI_API_KEY is required for the OpenAI provider")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.timeout = timeout
        configured_ca = os.getenv("SSL_CERT_FILE")
        system_ca = "/etc/ssl/cert.pem"
        ca_file = configured_ca or (system_ca if Path(system_ca).is_file() else None)
        self.ssl_context = ssl.create_default_context(cafile=ca_file)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def model_for(self, assigned_model: str) -> str:
        return self.model

    def audit(self, agent: str, model: str, payload: dict[str, Any]) -> ModelAudit | None:
        if not self.enabled:
            return None
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": audit_system_prompt(agent)},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "agent_audit", "strict": True, "schema": AUDIT_SCHEMA},
            },
            "temperature": 0,
            "max_tokens": 256,
            "seed": 42,
        }
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(1, 4):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                    result = json.loads(response.read().decode("utf-8"))
                content = result["choices"][0]["message"].get("content") or ""
                audit = json.loads(content)
                status, issues = audit.get("status"), audit.get("issues", [])
                if status not in {"accept", "review"} or not isinstance(issues, list):
                    raise ValueError(f"invalid structured audit: {audit!r}")
                usage = result.get("usage", {})
                return ModelAudit(
                    model=result.get("model", model),
                    provider="openai",
                    status=status,
                    issues=[str(value)[:200] for value in issues[:5]],
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    attempts=attempt,
                )
            except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
        if self.mode == "required":
            raise ModelRuntimeError(f"{agent} could not call {model} after 3 attempts: {last_error}") from last_error
        return ModelAudit(
            model=model,
            provider="openai",
            status="runtime_error",
            issues=[str(last_error)[:200]],
            prompt_tokens=None,
            completion_tokens=None,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            attempts=3,
        )


class HuggingFaceMLXRuntime:
    """Run a downloaded Hugging Face Qwen3.5 checkpoint through MLX-VLM.

    MLX objects are loaded lazily so the deterministic pipeline and unit tests
    do not require Apple Silicon or the optional MLX dependencies. A lock keeps
    one shared model safe when case/investigator threads submit audits together.
    """

    def __init__(
        self,
        model_path: str = "models/Qwen3.5-9B-4bit",
        mode: str = "required",
        max_tokens: int = 256,
    ) -> None:
        if mode not in {"off", "optional", "required"}:
            raise ValueError(f"Unknown model mode: {mode}")
        self.model_path = str(Path(model_path))
        self.mode = mode
        self.max_tokens = max_tokens
        self._lock = threading.Lock()
        self._model: Any | None = None
        self._processor: Any | None = None
        self._config: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def model_for(self, assigned_model: str) -> str:
        return self.model_path

    def _load(self) -> None:
        if self._model is not None:
            return
        path = Path(self.model_path)
        if not path.is_dir():
            raise ModelRuntimeError(
                f"Hugging Face model directory does not exist: {path}. "
                "Download mlx-community/Qwen3.5-9B-4bit first."
            )
        try:
            from mlx_vlm import load
            from mlx_vlm.utils import load_config
        except ImportError as exc:
            raise ModelRuntimeError(
                "mlx-vlm is required for the Hugging Face MLX provider; "
                "install the 'apple-mlx' project extra"
            ) from exc
        self._model, self._processor = load(str(path))
        self._config = load_config(path)

    def _generate(self, agent: str, payload: dict[str, Any]) -> Any:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        messages = [
            {"role": "system", "content": audit_system_prompt(agent)},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        prompt = apply_chat_template(
            self._processor,
            self._config,
            messages,
            num_images=0,
            enable_thinking=False,
        )
        return generate(
            self._model,
            self._processor,
            prompt,
            image=None,
            verbose=False,
            temperature=0.0,
            max_tokens=self.max_tokens,
        )

    def audit(self, agent: str, model: str, payload: dict[str, Any]) -> ModelAudit | None:
        if not self.enabled:
            return None
        started = time.perf_counter()
        last_error: Exception | None = None
        # MLX generation uses shared Metal/model state, so load and generation
        # are deliberately serialized while the surrounding agent workflow stays concurrent.
        with self._lock:
            for attempt in range(1, 3):
                try:
                    self._load()
                    result = self._generate(agent, payload)
                    status, issues = _parse_audit_json(result.text)
                    return ModelAudit(
                        model=model,
                        provider="huggingface-mlx",
                        status=status,
                        issues=issues,
                        prompt_tokens=getattr(result, "prompt_tokens", None),
                        completion_tokens=getattr(result, "generation_tokens", None),
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                        attempts=attempt,
                    )
                except Exception as exc:  # Optional runtime errors are recorded in trace.
                    last_error = exc
        if self.mode == "required":
            raise ModelRuntimeError(
                f"{agent} could not call local Hugging Face model {model} after 2 attempts: {last_error}"
            ) from last_error
        return ModelAudit(
            model=model,
            provider="huggingface-mlx",
            status="runtime_error",
            issues=[str(last_error)[:200]],
            prompt_tokens=None,
            completion_tokens=None,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            attempts=2,
        )
