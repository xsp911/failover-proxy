"""Protocol-preserving, whole-answer buffered failover. No LiteLLM dependency."""
from __future__ import annotations

import asyncio
import codecs
import copy
import ipaddress
import json
import socket
import time
import uuid
from dataclasses import dataclass, field
from collections import OrderedDict
from contextlib import asynccontextmanager

from .outbound_proxy import resolve_proxy
from .proxy_transport import async_client, proxy_fingerprint
from urllib.parse import urlsplit

import httpx

from .config import public_ip
from .diagnostics import diagnostic, classify, read_error, upstream_label


class GatewayError(Exception):
    def __init__(self, kind, status=502, retryable=True, detail=None):
        super().__init__(kind)
        self.kind, self.status, self.retryable = kind, status, retryable
        self.detail = detail if detail is not None else diagnostic(kind)

# finish_reason values that mean "the upstream finished on its own". A value outside this set
# is only fatal when the choice carries no content and no tool calls: an upstream answered
# HTTP 200 with a complete body and a reason outside the five-value whitelist, and the relay
# graded the answer as broken and failed over. Novel labels keep arriving (end_turn,
# max_tokens, eos, stop_sequence), so completeness is decided by the payload, not the vocabulary.
FINISH_REASONS_OK = {"stop", "tool_calls", "function_call", "length", "content_filter",
                     "end_turn", "max_tokens", "eos", "stop_sequence", "model_length"}

# Errors whose own remedy is "resend the request": the stream had started (HTTP 200, SSE
# began) and the upstream dropped it — A6API says so verbatim for
# smart_route_stream_commit_failure, which classify() maps to upstream_sse_error. A provider
# that aborted one stream is not a broken provider, and switching away drops the prompt cache.
STREAM_ABORT_KINDS = {"upstream_sse_error", "incomplete_upstream_stream",
                      "upstream_timeout", "upstream_connection"}


def response_format_of(payload):
    """The response_format type the client asked for, or None.

    Passed to the classifier so a provider refusing structured output is reported as
    `response_format_unsupported` instead of a vague 400 — but only when the client
    actually asked for structured output.
    """
    value = payload.get("response_format") if isinstance(payload, dict) else None
    if isinstance(value, dict):
        kind = value.get("type")
        return kind if isinstance(kind, str) else None
    return None


def banner(text):
    import re
    text = re.sub(r"\s+", " ", re.sub(r"[*#_`]", "", text[:4096])).strip().lower()
    return (text.startswith("ai provider temporarily unavailable")
            and all(k in text for k in ("request id:", "ref:", "model:"))
            and bool(re.search(r"upstreamresponsecode\s*:\s*[45]\d{2}\b", text)))


def token_number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_values(usage):
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details") or {}
    details = details if isinstance(details, dict) else {}
    inp = token_number(usage.get("prompt_tokens", usage.get("input_tokens")))
    out = token_number(usage.get("completion_tokens", usage.get("output_tokens")))
    read = token_number(details.get("cached_tokens", usage.get("cache_read_input_tokens")))
    write = token_number(usage.get("cache_creation_input_tokens", usage.get("cache_write_tokens")))
    return {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": read, "cache_write_tokens": write}


@dataclass
class Completion:
    model: str
    maximum: int
    max_events: int
    events: list[dict] = field(default_factory=list)
    seen: set = field(default_factory=set)
    finished: dict = field(default_factory=dict)
    texts: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    bytes: int = 0
    done: bool = False
    first_content_ms: float | None = None
    response_format: str | None = None
    start: float = field(default_factory=time.monotonic)

    def add(self, item):
        if not isinstance(item, dict):
            raise GatewayError("invalid_upstream_response")
        if "error" in item:
            raise GatewayError(*classify(item, 200, response_format=self.response_format))
        choices = item.get("choices")
        if not isinstance(choices, list):
            raise GatewayError("invalid_upstream_response")
        if len(self.events) >= self.max_events:
            raise GatewayError("response_event_limit")
        for choice in choices:
            if not isinstance(choice, dict):
                raise GatewayError("invalid_upstream_response")
            index = choice.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 127:
                raise GatewayError("invalid_choice_index")
            self.seen.add(index)
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise GatewayError("invalid_upstream_response")
            generated = any(delta.get(k) for k in ("content", "reasoning_content", "reasoning", "tool_calls", "function_call"))
            if generated and index in self.finished:
                raise GatewayError("content_after_finish")
            if generated and self.first_content_ms is None:
                self.first_content_ms = (time.monotonic() - self.start) * 1000
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise GatewayError("invalid_upstream_response")
            if content:
                self.texts[index] = (self.texts.get(index, "") + content)[:4096]
                if banner(self.texts[index]):
                    raise GatewayError("provider_error_banner", 503)
            calls = delta.get("tool_calls") or []
            if not isinstance(calls, list):
                raise GatewayError("invalid_tool_call")
            for call in calls:
                if not isinstance(call, dict):
                    raise GatewayError("invalid_tool_call")
                ti = call.get("index", 0)
                if isinstance(ti, bool) or not isinstance(ti, int) or not 0 <= ti <= 127:
                    raise GatewayError("invalid_tool_call")
                saved = self.tools.setdefault((index, ti), {"name": "", "arguments": "", "id": ""})
                fun = call.get("function") or {}
                if not isinstance(fun, dict):
                    raise GatewayError("invalid_tool_call")
                for key in ("name", "arguments"):
                    value = fun.get(key)
                    if value is not None:
                        if not isinstance(value, str):
                            raise GatewayError("invalid_tool_call")
                        saved[key] += value
                if call.get("id"):
                    saved["id"] = call["id"]
            reason = choice.get("finish_reason")
            if reason is not None:
                if reason not in FINISH_REASONS_OK:
                    has_payload = bool(self.texts.get(index)) or any(
                        key[0] == index for key in self.tools)
                    if not has_payload:
                        raise GatewayError("invalid_finish_reason")
                self.finished[index] = reason
        if isinstance(item.get("usage"), dict):
            self.usage = item["usage"]
        value = copy.deepcopy(item)
        value["model"] = self.model
        try:
            json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
        except (UnicodeEncodeError,ValueError,RecursionError):
            raise GatewayError('invalid_upstream_encoding') from None
        self.events.append(value)

    def validate(self):
        if not self.seen or not self.seen.issubset(self.finished):
            raise GatewayError("incomplete_upstream_stream")
        for (index, _), call in self.tools.items():
            # length/content_filter are explicit upstream termination, not transport
            # failures. Never conceal or repeatedly retry those provider decisions.
            if self.finished[index] in ("length", "content_filter"):
                continue
            if not call["name"] or not isinstance(call["id"], str) or not call["id"]:
                raise GatewayError("invalid_tool_call")
            try:
                args = json.loads(call["arguments"])
            except (ValueError, TypeError):
                raise GatewayError("invalid_tool_arguments") from None
            if not isinstance(args, dict):
                raise GatewayError("invalid_tool_arguments")

    def wire(self):
        for item in self.events:
            yield b"data: " + json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"
        yield b"data: [DONE]\n\n"


async def parse_sse(response, completion):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = ""
    data = []
    trailing_cr = False

    def line(value):
        if value == "":
            if not data:
                return
            payload = "\n".join(data)
            data.clear()
            if payload == "[DONE]":
                completion.done = True
                return
            if completion.done:
                raise GatewayError("event_after_done")
            try:
                item = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
            except (ValueError, TypeError, RecursionError):
                raise GatewayError("invalid_sse_json") from None
            completion.add(item)
        elif value.startswith("data:"):
            data.append(value[5:].removeprefix(" "))
        elif value.startswith(":") or value.startswith(("event:", "id:", "retry:")):
            return
        else:
            raise GatewayError("invalid_sse_frame")

    try:
        async for raw in response.aiter_bytes():
            completion.bytes += len(raw)
            if completion.bytes > completion.maximum:
                raise GatewayError("response_size_limit")
            text = decoder.decode(raw)
            if trailing_cr:
                text = text.removeprefix("\n")
                trailing_cr = False
            if text.endswith("\r"):
                trailing_cr = True
            pending += text.replace("\r\n", "\n").replace("\r", "\n")
            while "\n" in pending:
                value, pending = pending.split("\n", 1)
                line(value)
        pending += decoder.decode(b"", final=True)
        # A complete final event without final blank line is accepted if valid;
        # truncated JSON/choice completion is still rejected.
        if pending:
            line(pending)
        if data:
            line("")
    except UnicodeDecodeError:
        raise GatewayError("invalid_sse_encoding") from None
    completion.validate()
    return completion


async def destination(url, private):
    """Resolve once then pin HTTP connection IP; retain Host/TLS SNI hostname."""
    p = urlsplit(url)
    if private:
        return url, {}, {}
    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(loop.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM), 5)
    except (OSError, TimeoutError):
        raise GatewayError("dns_failure", 503) from None
    addresses = list(dict.fromkeys(r[4][0] for r in records))
    if not addresses or not all(public_ip(x) for x in addresses):
        raise GatewayError("unsafe_upstream_address", 502, False)
    chosen = addresses[0]
    pinned = str(httpx.URL(url).copy_with(host=chosen))
    return pinned, {"Host": p.netloc}, {"sni_hostname": p.hostname}


class Engine:
    def __init__(self, store, transport=None):
        self.store = store
        self.capabilities=getattr(store,'capabilities',None)
        # DNS-pinned IP origins must never share persistent TLS connections
        # across distinct configured hostnames/SNI virtual hosts.
        self.transport = transport
        self.client = self._new_client()
        self.proxy_clients = OrderedDict()
        self._retired_proxy_clients = []
        self.cooldowns = {}
        self.failures = {}
        self.active = 0

    def _new_client(self, profile=None):
        return async_client(profile, self.transport,
            limits=httpx.Limits(max_connections=80, max_keepalive_connections=0),
            headers={"User-Agent": "FailoverProxy/0.1"})

    @asynccontextmanager
    async def provider_client(self, provider, config=None):
        """Lease a credential-aware client; rotation never closes active requests."""
        try:
            if provider.get('proxy_id') and config is None:
                config = await asyncio.to_thread(self.store.get_config, True)
            profile = resolve_proxy(config or {}, provider.get('proxy_id'))
            key = proxy_fingerprint(profile)
            entry = self.proxy_clients.get(key)
            if profile is not None and entry is None:
                entry = {'client': self._new_client(profile), 'id': profile['id'], 'users': 0, 'retired': False}
        except (ValueError, ImportError):
            raise GatewayError('outbound_proxy_unavailable', 502, False) from None
        if profile is None:
            yield self.client
            return
        retired = []
        for old_key, old in list(self.proxy_clients.items()):
            if old_key != key and old['id'] == profile['id']:
                retired.append(self.proxy_clients.pop(old_key))
        self.proxy_clients[key] = entry
        self.proxy_clients.move_to_end(key)
        entry['users'] += 1
        while len(self.proxy_clients) > 32:
            _, old = self.proxy_clients.popitem(last=False)
            retired.append(old)
        for old in retired:
            old['retired'] = True
            self._retired_proxy_clients.append(old)
        try:
            for old in retired:
                if not old['users']:
                    await old['client'].aclose()
                    self._retired_proxy_clients.remove(old)
            yield entry['client']
        finally:
            entry['users'] -= 1
            if entry['retired'] and not entry['users']:
                await entry['client'].aclose()
                self._retired_proxy_clients.remove(entry)

    async def close(self):
        await self.client.aclose()
        entries = list(self.proxy_clients.values()) + self._retired_proxy_clients
        self.proxy_clients.clear()
        self._retired_proxy_clients = []
        for entry in entries:
            await entry['client'].aclose()

    def runtime(self):
        return {id: {"cooldown_until": ts} for id, ts in self.cooldowns.items()}

    async def test_provider(self, provider, settings, config=None):
        start = time.monotonic()
        try:
            url, headers, extensions = await destination(provider["base_url"] + "/models", settings["trust_private_upstreams"])
            headers["Authorization"] = "Bearer " + provider["api_key"]
            async with self.provider_client(provider, config) as client, client.stream("GET", url, headers=headers, extensions=extensions, timeout=10) as r:
                return {"ok": r.status_code == 200, "status": r.status_code, "latency_ms": round((time.monotonic()-start)*1000),
                        **({"error_kind": "upstream_http_error"} if r.status_code != 200 else {})}
        except (GatewayError, httpx.HTTPError, TimeoutError):
            return {"ok": False, "status": None, "latency_ms": round((time.monotonic()-start)*1000), "error_kind": "connection_failed"}

    async def generate(self, payload, config, request_id, key_id, attribution=None, *, probe=False):
        settings = config["settings"]
        if self.active >= settings["max_concurrent_requests"]:
            raise GatewayError("gateway_capacity", 429, False)
        self.active += 1
        start = time.monotonic()
        selected = None
        count = 0
        final = {"id": request_id, "time": time.time(), "model": payload["model"], "status": "error", "key_id": key_id}
        try:
            model = next((x for x in config["models"] if x["name"] == payload["model"] and x["enabled"]), None)
            if not model:
                raise GatewayError("model_not_found", 404, False)
            from .context_preflight import observe_context
            context_observation=observe_context(payload,model)
            if context_observation is not None:final['context_preflight']=context_observation
            providers = {x["id"]: x for x in config["providers"] if x["enabled"] and x.get("api_key")}
            routes = sorted((r for r in model["routes"] if r["enabled"] and r["provider_id"] in providers), key=lambda r:(providers[r["provider_id"]]["priority"],r["provider_id"]))
            routes = [r for r in routes if self.cooldowns.get(r["provider_id"], 0) <= time.time()]
            if not routes:
                raise GatewayError("no_available_provider", 503)
            last_error = GatewayError("all_providers_failed", 503)
            async with asyncio.timeout(settings["request_timeout_s"]):
                # One resend to the SAME provider before failing over: a mid-stream abort is
                # the upstream's bad minute and its own documented cure is a retry. Switching
                # instead drops the warm prompt cache and charges a provider that did not fail.
                queue = list(routes[:settings["max_attempts"]])
                resent = set()
                while queue:
                    route = queue.pop(0)
                    count += 1
                    p = providers[route["provider_id"]]
                    selected = p["id"]
                    attempt_start = time.monotonic()
                    event = {"id": str(uuid.uuid4()), "request_id": request_id, "provider_id": selected,
                             "ts": time.time(), "ordinal": count, "status": "error"}
                    result = None
                    try:
                        from .provider_policy import apply_provider_policy
                        try:outgoing = apply_provider_policy(payload,p)
                        except ValueError as conflict:raise GatewayError(str(conflict),400,False) from None
                        outgoing["model"] = route["upstream_model"]
                        # Native JSON forwarding preserves thinking/reasoning/images/tools.
                        url, headers, extensions = await destination(p["base_url"] + "/chat/completions", settings["trust_private_upstreams"])
                        headers.update({"Authorization": "Bearer " + p["api_key"], "Content-Type": "application/json", "Accept-Encoding":"identity"})
                        timeout = httpx.Timeout(settings["read_timeout_s"], connect=settings["connect_timeout_s"])
                        async with self.provider_client(p, config) as client, client.stream("POST", url, json=outgoing, headers=headers, extensions=extensions, timeout=timeout) as response:
                            event["http_status"] = response.status_code
                            if response.status_code != 200:
                                diagnostic_payload = await read_error(response)
                                raise GatewayError(*classify(diagnostic_payload, response.status_code,
                                                            response_format=response_format_of(payload)))
                            if response.headers.get('content-encoding','identity').lower() not in ('identity',''):
                                raise GatewayError('compressed_upstream_response')
                            if payload.get("stream", False):
                                if "text/event-stream" not in response.headers.get("content-type", "").lower():
                                    raise GatewayError("invalid_stream_content_type")
                                collector = Completion
                                if probe:
                                    from .probe_stream import ProbeCompletion
                                    collector = ProbeCompletion
                                result = collector(model["name"], settings["max_response_bytes"], settings["max_buffer_events"], start=attempt_start,
                                                   response_format=response_format_of(payload))
                                await parse_sse(response, result)
                                event.update(usage_values(result.usage))
                                event["ttft_ms"] = result.first_content_ms
                            else:
                                raw = bytearray()
                                async for part in response.aiter_bytes():
                                    raw.extend(part)
                                    if len(raw) > settings["max_response_bytes"]:
                                        raise GatewayError("response_size_limit")
                                try:
                                    result = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
                                except (ValueError, UnicodeDecodeError, RecursionError):
                                    raise GatewayError("invalid_upstream_json") from None
                                self.validate_json(result, model["name"], response_format_of(payload))
                                event.update(usage_values(result.get("usage")))
                        event["status"] = "success"
                        elapsed = (time.monotonic()-attempt_start)*1000
                        event["generation_ms"] = max(0,elapsed-event["ttft_ms"]) if event.get("ttft_ms") is not None else None
                        self.failures[selected] = 0
                        self.cooldowns.pop(selected, None)
                        final.update({"status": "success", **{k:event.get(k) for k in ("input_tokens","output_tokens","cache_read_tokens","cache_write_tokens")}})
                        if attribution is not None:
                            attribution.update(upstream=upstream_label(p),provider_id=p['id'])
                        return result
                    except asyncio.CancelledError:
                        event.update(status="cancelled", error_kind="cancelled")
                        raise
                    except httpx.TimeoutException:
                        last_error = GatewayError("upstream_timeout", 504)
                    except httpx.TransportError:
                        last_error = GatewayError("upstream_connection", 502)
                    except GatewayError as e:
                        last_error = e
                    finally:
                        event["latency_ms"] = round((time.monotonic()-attempt_start)*1000, 2)
                        if event["status"] == "error":
                            event["error_kind"] = last_error.kind
                            # The fixed diagnostic text, never the upstream's free-form body:
                            # error_kind alone cannot tell a context overflow from a stream
                            # abort when you read the attempts table.
                            event["error_detail"] = last_error.detail
                        await asyncio.to_thread(self.store.record_attempt, event)
                        if self.capabilities is not None:
                            from .capability_observer import observe
                            raw_usage=result.usage if isinstance(result,Completion) else result.get('usage',{}) if isinstance(result,dict) else {}
                            finish=result.finished.get(0) if isinstance(result,Completion) else (result.get('choices') or [{}])[0].get('finish_reason') if isinstance(result,dict) else None
                            await asyncio.to_thread(observe,self.capabilities,provider=p,model=route['upstream_model'],protocol='chat',request_id=request_id,source='fingerprint' if probe else 'gateway',payload=payload,outcome=event['status'],http_status=event.get('http_status'),usage=raw_usage,finish_reason=finish,error_kind=event.get('error_kind'),context=context_observation)
                    if not last_error.retryable:
                        # Input/context rejection is not a provider outage.
                        raise last_error
                    is_abort = (event.get("http_status") == 200
                                and last_error.kind in STREAM_ABORT_KINDS)
                    if is_abort and selected not in resent:
                        # The stream had started and died; A6API's own remedy is a resend.
                        resent.add(selected)
                        queue.insert(0, route)
                        await asyncio.sleep(2)
                        continue
                    if last_error.kind != "dns_failure":
                        # dns_failure is this host's resolver, not the provider's health:
                        # fail over if another resolves, but do not cool the provider down.
                        self.failures[selected] = self.failures.get(selected, 0) + 1
                        if self.failures[selected] >= settings["failure_threshold"]:
                            self.cooldowns[selected] = time.time() + settings["cooldown_s"]
                raise last_error
        except TimeoutError:
            final["error_kind"] = "request_deadline"
            raise GatewayError("request_deadline", 504) from None
        except asyncio.CancelledError:
            final.update(status="cancelled", error_kind="cancelled")
            raise
        except GatewayError as e:
            final["error_kind"] = e.kind
            raise
        finally:
            self.active -= 1
            final.update(attempts=count, provider_id=selected, total_ms=round((time.monotonic()-start)*1000,2))
            if final["status"] == "success":
                final["client_ttft_ms"] = final["total_ms"]
            await asyncio.to_thread(self.store.record_request, final)

    @staticmethod
    def validate_json(result, model, response_format=None):
        if isinstance(result, dict) and 'error' in result:
            raise GatewayError(*classify(result,200,response_format=response_format))
        if not isinstance(result, dict) or not isinstance(result.get("choices"),list) or not result["choices"]:
            raise GatewayError("invalid_upstream_response")
        for choice in result["choices"]:
            if not isinstance(choice, dict):
                raise GatewayError("invalid_upstream_response")
            if choice.get("finish_reason") not in FINISH_REASONS_OK:
                message = choice.get("message")
                has_payload = isinstance(message, dict) and (
                    (isinstance(message.get("content"), str) and message["content"])
                    or message.get("tool_calls"))
                if not has_payload:
                    raise GatewayError("incomplete_upstream_response")
            message = choice.get("message")
            if not isinstance(message,dict):
                raise GatewayError("invalid_upstream_response")
            if isinstance(message.get("content"),str) and banner(message["content"]):
                raise GatewayError("provider_error_banner",503)
            if choice["finish_reason"] not in ("length","content_filter"):
                calls = message.get("tool_calls") or []
                if not isinstance(calls,list):
                    raise GatewayError("invalid_tool_arguments")
                for tool in calls:
                    try:
                        if not tool.get("id") or not tool["function"].get("name") or not isinstance(json.loads(tool["function"]["arguments"]),dict):
                            raise ValueError()
                    except (ValueError,TypeError,KeyError,AttributeError):
                        raise GatewayError("invalid_tool_arguments") from None
        result["model"] = model
        try:
            json.dumps(result,ensure_ascii=False,allow_nan=False).encode('utf-8')
        except (UnicodeEncodeError,ValueError,RecursionError):
            raise GatewayError('invalid_upstream_encoding') from None
