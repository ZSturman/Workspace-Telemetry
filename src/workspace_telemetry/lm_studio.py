"""OpenAI-compatible LM Studio client for structured telemetry summaries."""

from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import GenerationResult, SummaryContent
from .strategies import SYSTEM_PROMPT


class LMStudioError(RuntimeError):
    pass


class LMStudioClient:
    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        timeout_seconds: int,
        retries: int,
        response_format: str = "json_object",
        request: Callable[[str, str, dict[str, Any] | None], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.response_format = response_format if response_format in {"json_object", "json_schema"} else "json_object"
        self._active_timeout_seconds = float(timeout_seconds)
        self._request_impl = request or self._request
        self._sleep = sleep

    def summarize(self, payload: dict[str, Any]) -> GenerationResult:
        started_at = time.monotonic()
        self._active_timeout_seconds = float(self.timeout_seconds)
        endpoint, model = self.discover()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)},
        ]
        raw = self._chat(endpoint, model, messages)
        try:
            return GenerationResult(_validate_response(raw), True, endpoint, model)
        except (ValueError, json.JSONDecodeError) as first_error:
            # Retry the original compact facts once. Empty/truncated output is
            # never fed back to the model and is never re-extracted here.
            remaining = self.timeout_seconds - (time.monotonic() - started_at)
            # Unit-scale deadlines and fast local failures still have enough
            # budget for the single bounded repair.  Only skip it when the
            # original request genuinely exhausted the shared deadline.
            if remaining <= 0.05:
                raise first_error
            self._active_timeout_seconds = remaining
            try:
                repaired = self._chat(endpoint, model, [
                    {"role": "system", "content": SYSTEM_PROMPT + "\nThis is a repair attempt. Emit the complete compact JSON object immediately."},
                    messages[1],
                ])
                return GenerationResult(_validate_response(repaired), True, endpoint, model)
            finally:
                self._active_timeout_seconds = float(self.timeout_seconds)

    def discover(self) -> tuple[str, str]:
        errors: list[str] = []
        for root in _candidate_roots(self.endpoint):
            try:
                payload = self._retry("GET", f"{root}/models", None)
                rows = payload.get("data", []) if isinstance(payload, dict) else []
                model_ids = [str(row.get("id") or "") for row in rows if isinstance(row, dict) and row.get("id")]
                if not model_ids:
                    raise LMStudioError("LM Studio returned no loaded models.")
                if self.model and self.model not in model_ids:
                    raise LMStudioError(f"Configured LM Studio model {self.model!r} is not loaded.")
                return root, self.model or model_ids[0]
            except LMStudioError as exc:
                errors.append(str(exc))
        raise LMStudioError("LM Studio unavailable: " + "; ".join(errors))

    def _chat(self, root: str, model: str, messages: list[dict[str, str]]) -> Any:
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            # Reasoning-capable local models can spend hundreds of tokens before
            # emitting the compact JSON answer.  Keep this high enough for the
            # final object; scheduled telemetry never enters this path.
            "max_tokens": 1200,
            "stream": False,
            "response_format": _response_format(self.response_format),
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
            "enableThinking": False,
            "enable_thinking": False,
        }
        try:
            return self._retry("POST", f"{root}/chat/completions", body)
        except LMStudioError as exc:
            if "HTTP 400" not in str(exc) and "HTTP 422" not in str(exc):
                raise
            if self.response_format == "json_schema":
                body["response_format"] = {"type": "text"}
            else:
                body["response_format"] = _response_format("json_schema")
            return self._retry("POST", f"{root}/chat/completions", body)

    def _retry(self, method: str, path: str, payload: dict[str, Any] | None) -> Any:
        last_error: LMStudioError | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._request_impl(method, path, payload)
            except LMStudioError as exc:
                last_error = exc
                if attempt >= self.retries or not _retryable(exc):
                    break
                self._sleep(float(2 ** attempt))
        raise last_error or LMStudioError("LM Studio request failed.")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(path, data=body, headers={"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=max(1.0, self._active_timeout_seconds)) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise LMStudioError(f"HTTP {exc.code}: {text[:300]}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LMStudioError(str(exc)) from exc


def _candidate_roots(endpoint: str) -> list[str]:
    raw = endpoint.rstrip("/")
    if raw.endswith("/chat/completions"):
        raw = raw[: -len("/chat/completions")]
    if not raw.endswith("/v1"):
        raw = f"{raw}/v1"
    candidates = [raw]
    if "localhost" in raw:
        candidates.append(raw.replace("localhost", "127.0.0.1"))
    return list(dict.fromkeys(candidates))


def _retryable(error: LMStudioError) -> bool:
    text = str(error)
    return not any(f"HTTP {status}" in text for status in (400, 401, 403, 404, 422))


def _extract_content(response: Any) -> str:
    if not isinstance(response, dict):
        raise ValueError("LM Studio response was not an object.")
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("LM Studio returned no choices.")
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    content = message.get("content") or choices[0].get("text")
    if isinstance(content, list):
        content = "\n".join(str(part.get("text") or part.get("content") or "") if isinstance(part, dict) else str(part) for part in content)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LM Studio returned empty content.")
    text = content.strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return text


def _validate_response(response: Any) -> SummaryContent:
    if not isinstance(response, dict):
        raise ValueError("LM Studio response was not an object.")
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("LM Studio returned no choices.")
    finish_reason = str(choices[0].get("finish_reason") or "").casefold()
    if finish_reason in {"length", "max_tokens"}:
        raise ValueError("LM Studio output was truncated at its completion limit.")
    return _validate_content(_extract_content(response))


def _validate_content(raw: str) -> SummaryContent:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("LM Studio summary must be an object.")
    required = {
        "headline": 90,
        "recap": 700,
        "onTaskAssessment": 360,
        "switchingAssessment": 360,
        "distractionAssessment": 360,
        "progressAssessment": 500,
    }
    values: dict[str, str] = {}
    for key, limit in required.items():
        if not isinstance(payload.get(key), str):
            raise ValueError(f"LM Studio field {key} must be text.")
        values[key] = _text(payload[key], limit)
        if not values[key]:
            raise ValueError(f"LM Studio field {key} is missing.")
    raw_confidence = payload.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise ValueError("LM Studio confidence must be numeric.")
    confidence = float(raw_confidence)
    return SummaryContent(
        summary=values["recap"],
        primary_activity="",
        work_completed="No completion is asserted from activity telemetry.",
        work_in_progress=values["progressAssessment"],
        observations=(values["onTaskAssessment"], values["switchingAssessment"], values["distractionAssessment"]),
        confidence=max(0.0, min(1.0, confidence)),
        headline=values["headline"],
        recap=values["recap"],
        on_task_assessment=values["onTaskAssessment"],
        switching_assessment=values["switchingAssessment"],
        distraction_assessment=values["distractionAssessment"],
        progress_assessment=values["progressAssessment"],
    )


def _response_format(mode: str) -> dict[str, Any]:
    if mode != "json_schema":
        return {"type": "json_object"}
    properties = {
        key: {"type": "string"}
        for key in (
            "headline", "recap", "onTaskAssessment", "switchingAssessment",
            "distractionAssessment", "progressAssessment",
        )
    }
    properties["confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "telemetry_recap",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _strings(value: Any, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        text = _text(item, 160)
        if text and text not in result:
            result.append(text)
    return tuple(result[:limit])
