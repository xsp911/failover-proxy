#!/usr/bin/env python3
"""Measure how many output tokens a channel actually allows.

A channel that truncates at 32k while the model's real ceiling is 200k looks exactly like a
model that "answers badly": you ask for more than the channel allows, you get a cut-off
answer, and `finish_reason` says `length`. This tool finds the real number once, so callers
can clamp to it instead of guessing.

It is deliberately standalone: stdlib only, no project imports, no database. Point it at any
OpenAI-compatible endpoint.

    python3 tools/probe/ceiling.py --base-url https://api.example.com/v1 \
        --model deepseek-v4.1-flash-1017 --key-env MY_API_KEY

The search asks for a long, deterministic output and reads `finish_reason`:
`stop` means the request fitted, `length` means it was cut. A 400/422 on `max_tokens` means
the channel rejects the value outright — also a ceiling signal. Binary search narrows it to
the largest value that still finishes naturally.

The key is read from the environment by NAME and never printed or logged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Asking for a long answer that the model will actually produce: counting is deterministic
# and does not invite the model to summarise, which would end the answer early and look like
# a channel ceiling when it is model behaviour.
PROMPT = ("Count from 1 to 4000 in words, one number per line, no commentary. "
          "Do not stop early and do not summarise.")


def ask(base_url: str, model: str, key: str, max_tokens: int, timeout: float) -> dict:
    """One request. Returns {'fitted': bool, 'completion_tokens': int, 'reason': str}."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read()[:200].decode("utf-8", "replace")
        # A channel that refuses the parameter is telling us the value is above its ceiling.
        return {"fitted": False, "completion_tokens": 0,
                "reason": f"http_{error.code}", "detail": detail}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        # A transport failure says nothing about the ceiling: report it and let the caller
        # decide, rather than recording "0 tokens" as if the channel had answered.
        return {"fitted": False, "completion_tokens": 0,
                "reason": "transport", "detail": f"{type(error).__name__}: {error}"[:200]}
    choice = (payload.get("choices") or [{}])[0]
    reason = choice.get("finish_reason")
    used = (payload.get("usage") or {}).get("completion_tokens") or 0
    return {"fitted": reason in ("stop", "end_turn", "eos", "stop_sequence"),
            "completion_tokens": used, "reason": str(reason)}


def measure(base_url: str, model: str, key: str, low: int, high: int, timeout: float,
            quiet: bool = False) -> dict:
    """Largest `max_tokens` the channel serves without truncating."""
    steps = []
    # A channel that fails at the top of the range cannot serve anything above `low`.
    top = ask(base_url, model, key, high, timeout)
    steps.append({"max_tokens": high, **top})
    if top["fitted"]:
        return {"ceiling": high, "steps": steps, "note": "the whole range fits"}
    if top["reason"] == "transport":
        # Nothing to narrow: the endpoint did not answer at all. Say so instead of spending
        # eight more requests proving it.
        return {"ceiling": None, "steps": steps,
                "error": top.get("detail") or "the endpoint did not answer"}
    best = 0
    lo, hi = low, high
    while lo <= hi:
        mid = (lo + hi) // 2
        result = ask(base_url, model, key, mid, timeout)
        steps.append({"max_tokens": mid, **result})
        if not quiet:
            print(f"  max_tokens={mid:<7} reason={result['reason']:<12} "
                  f"completion={result['completion_tokens']}", file=sys.stderr)
        if result["fitted"]:
            best = max(best, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    return {"ceiling": best or None, "steps": steps}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base, e.g. https://host/v1")
    parser.add_argument("--model", required=True, help="model id to probe")
    parser.add_argument("--key-env", default="A6API_KEY",
                        help="environment variable holding the API key (never printed)")
    parser.add_argument("--low", type=int, default=1024, help="lower bound of the search")
    parser.add_argument("--high", type=int, default=262144, help="upper bound of the search")
    parser.add_argument("--timeout", type=float, default=600.0, help="per-request timeout, seconds")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)

    key = os.environ.get(args.key_env)
    if not key:
        print(f"error: environment variable {args.key_env} is empty or unset", file=sys.stderr)
        return 2
    if args.low > args.high:
        print("error: --low must not exceed --high", file=sys.stderr)
        return 2

    print(f"probing {args.model} at {args.base_url}", file=sys.stderr)
    report = measure(args.base_url, args.model, key, args.low, args.high, args.timeout)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif report["ceiling"]:
        print(f"ceiling: {report['ceiling']} output tokens")
    elif report.get("error"):
        print(f"ceiling: not measured — {report['error']}", file=sys.stderr)
    else:
        print("ceiling: not measured — the channel refused every value in the range",
              file=sys.stderr)
    return 0 if report["ceiling"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
