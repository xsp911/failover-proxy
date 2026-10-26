"""Allowlisted upstream diagnostics: no arbitrary provider text reaches clients."""
import asyncio
import json
import re

LIMIT = 8192
READ_TIMEOUT = 2.0
CONTEXT_CODES = {"context_length_exceeded", "context_window_exceeded", "max_context_length_exceeded", "prompt_too_long", "input_too_long"}
CONTEXT_PATTERNS = (
    r"\bmaximum context length\b", r"\bcontext (?:window|length) (?:is )?exceeded\b",
    r"\bexceeds? (?:the )?(?:maximum |model(?:'s)? )?context (?:window|length)\b",
    r"\btoo many (?:input |prompt )?tokens\b", r"\b(?:prompt|input) (?:is )?too long\b",
)
# Full-message allowlist only: arbitrary mentions of timeout may echo user input.
TIMEOUT_PATTERNS = (
    r"upstream request timed out\.?", r"upstream timed out\.?", r"upstream timeout\.?",
)
DETAILS = {
    "upstream_timeout": "The upstream request timed out. Other configured routes may be tried.",
    "context_length_exceeded": "The request exceeds the upstream model's context window. Reduce the prompt or requested output; retrying the same request will not help.",
    "upstream_http_400": "The upstream rejected the request as invalid. Check the model's supported parameters and input format.",
    "upstream_http_413": "The upstream rejected the request size. Reduce the input size.",
    "upstream_http_422": "The upstream could not validate the request. Check the input and parameter values.",
    "upstream_http_401": "The configured upstream credential was rejected; contact the gateway administrator.",
    "upstream_http_403": "Access was denied by the upstream; contact the gateway administrator.",
    "upstream_http_429": "The upstream is rate limited. Retry after a short delay.",
    "upstream_sse_error": "The upstream returned an error inside the response stream; no partial response was delivered.",
    "gateway_capacity": "The gateway is busy. Retry after one second.",
}


def diagnostic(kind):
    if kind in DETAILS:
        return DETAILS[kind]
    if re.fullmatch(r"upstream_http_5\d\d", kind):
        return "The upstream reported a temporary server error. Other configured routes may be tried."
    return None


def classify(payload, status):
    """Classify only error fields; never include free-form message in output."""
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        code, message = err.get("code"), err.get("message")
    else:
        code, message = None, err if isinstance(err, str) else None
    # Exact structured timeout codes outrank free-form message heuristics.
    # Never let error bodies override authentication/client errors.
    if status in (200, 500) and isinstance(payload, dict):
        fields = [payload.get("code"), payload.get("type")]
        if isinstance(err, dict):
            fields.extend((err.get("code"), err.get("type")))
        if any(isinstance(value, str) and value == "upstream_timeout" for value in fields):
            return "upstream_timeout", 504, True, DETAILS["upstream_timeout"]
    if isinstance(code, str) and len(code) <= 100:
        known = code.lower() in CONTEXT_CODES
    else:
        known = False
    if isinstance(message, str):
        message = message[:4096].lower()
        known = known or any(re.search(p, message) for p in CONTEXT_PATTERNS)
    # An explicit context error in a 200 SSE error or 4xx is actionable.
    # A generic HTTP5xx remains a provider failure, never trust echoed prompt text.
    if known and (status == 200 or status in (400, 413, 422)):
        return "context_length_exceeded", 400, False, DETAILS["context_length_exceeded"]
    if status in (200, 500) and isinstance(payload, dict):
        timeout_message = err.get("message") if isinstance(err, dict) else err
        if not isinstance(timeout_message, str):
            timeout_message = payload.get("message")
        # Match the original complete message, not the truncated context scan.
        if isinstance(timeout_message, str) and any(
            re.fullmatch(pattern, timeout_message, re.IGNORECASE) for pattern in TIMEOUT_PATTERNS
        ):
            return "upstream_timeout", 504, True, DETAILS["upstream_timeout"]
    kind = "upstream_sse_error" if status == 200 else "upstream_http_" + str(status)
    retryable = status == 200 or status in (401,403,408,409,425,429) or status >= 500
    public_status = 502 if status == 200 or status not in (400,413,422,429) else status
    return kind, public_status, retryable, diagnostic(kind)


async def read_error(response):
    # Do not decompress unknown content encodings; don't accept HTML/plain text
    # as client diagnostics. At most LIMIT bytes and READ_TIMEOUT seconds.
    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
        return None
    raw = bytearray()
    try:
        if response.is_stream_consumed:
            # httpx MockTransport / already-buffered responses: no new I/O.
            raw.extend(response.content[:LIMIT])
        else:
            async with asyncio.timeout(READ_TIMEOUT):
                async for chunk in response.aiter_raw():
                    raw.extend(chunk[:max(0,LIMIT-len(raw))])
                    if len(raw) >= LIMIT:
                        break
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return value if isinstance(value, dict) else None
    except Exception:
        # CancelledError is BaseException: caller cancellation still propagates.
        return None


def upstream_label(provider):
    name = provider.get("name", "")
    if isinstance(name, str) and 0 < len(name) <= 100 and all(32 <= ord(x) <= 126 for x in name):
        return name
    return provider["id"]
