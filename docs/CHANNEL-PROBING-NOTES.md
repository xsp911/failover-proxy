# Channel probing notes

What ~130 probes of reseller channels taught us, written down so the next person does not
repeat the mistakes. Numbers come from two days of probing one model's lane; the method is
what transfers, not the counts.

## Why probe at all

A reseller listing says which model a channel claims to serve. That claim is not the product.
We observed, on the same model id, at the same time:

- a channel that answered with a different model than the one it sells;
- a channel that injected its own system prompt and answered as a coding assistant;
- a channel that silently truncated long answers at 32k tokens while the model's real
  ceiling was 200k;
- a channel that answered the first few requests honestly and degraded afterwards.

None of these is visible in a `/models` listing or a price field. They are visible only from
the contents of an answer, compared against a reference — which is what a probe does.

## Five checks, each catching a different lie

| check | what it catches | how |
|---|---|---|
| **fingerprint** | a different model behind the claimed id | send numeric challenges, score the replies locally against a bank of reference models; the result is `target_probability` for the claimed model |
| **prompt-token count** | prompt injection | compare the token count of a fixed prompt with the expected range |
| **drawing** | a model that passes numbers but is not the model | ask for a fixed structured artifact (we use an SVG pelican) and score it on two axes |
| **motion** | a static drawing passed off as an animated one | render four frames over time and require the object to move |
| **ceiling** | silent truncation | ask for the model's maximum output, then check `finish_reason` and the produced length |

One signal alone is fooled. A channel that proxies a cheap model can memorise numeric
challenges; a channel can serve the right model on a numeric prompt and a different one on a
creative prompt. Only the same channel, at the same minute, on several signals at once tells
you what a user would actually get.

## The signatures we found

| observation | verdict |
|---|---|
| `target_probability` ≥ 0.95 for the claimed model, prompt-token count in range | honest |
| prompt tokens ≈ 1003–1004 where a plain prompt costs 150–200 | **injection** — the channel prepends its own system prompt; nine channels in one lane |
| `target_probability` near 0 with a specific *other* model named as the top prediction | substitution |
| three different failures (timeout, 502, empty answer) on three attempts | dead |
| `finish_reason` = `length` at a suspiciously round number | ceiling below the advertised one |
| fewer valid samples than challenges | inconclusive, not a verdict |

The prompt-token check is the cheapest and the most decisive: an injected system prompt is
1.5–6× the size of the real task, and the number is reported by the upstream itself, so it
cannot be faked without removing the injection.

## A failure is not a property of the channel until you rule out the market and yourself

Three error classes belong to somebody else and must not be charged to the channel:

- **`pool_unavailable` / HTTP 503** — the reseller's merchant pool is briefly empty. The
  channel is fine; retry later.
- **`dns_failure`** — the client host's resolver. Never cool a provider down for this.
- **`cancelled`** — the client closed the connection. Our own decision, not a provider fault.

We learned this the hard way: a channel graded "dead" on three 503s turned out to be
perfectly healthy a day later, and another measurement was thrown away entirely because the
pool was down for the whole run.

The same applies to timeouts: after raising our own read timeout from 60 s to 300 s, most
"timeouts" on a low-scoring provider disappeared. Before blaming a provider, check what your
own client timeout is doing to long answers. On one provider, 41 of 42 failures were our own
artifacts (client timeout, a `finish_reason` we did not recognise, DNS) and its real failure
rate was 0.6%.

## Measure the ceiling per channel, do not assume it

Requesting more tokens than a channel allows produces a truncated answer that looks like a
model failure. We now measure the maximum output per channel once, store it, and clamp every
request to it. Two channels on the same model id differed by 6× (32,768 vs 200,000), and the
documentation named neither.

Related: a new `finish_reason` value you do not recognise is not a broken answer. Decide by
payload, not by vocabulary — content or tool calls present means the answer is usable.

## Two axes, not one score

We grade a drawing on two independent axes, each 0–10:

- **view** (does it look right) — composition, proportions, legibility;
- **work** (does it function) — geometry is closed, shapes are distinct, the animation moves.

A single score hides the interesting failure: a beautiful drawing with no motion, or a
correct animation of an unreadable object. The class gate (A/B/C) then maps the pair to a
decision, and it lives in exactly one place in code — earlier the gate was duplicated in SQL
and in Python, and the two disagreed.

## Money: price, cache, effective cost

Record price history, not the current price: a channel's price moves in both directions and
an old comparison silently becomes wrong. Track `cache_read`/`cache_write` prices separately
— on long agent turns the cache terms dominate, and a channel that looks cheap per token can
be the expensive one at 90% cache hit.

Effective cost per successful task (price ÷ success rate, cache included) is the number that
decides routing, not the headline price. The cheapest listing in a lane was not the cheapest
channel.

## How many probes before a verdict

Three independent confirmations, on different attempts, before writing a channel off. A
single honest sample after three failures is not redemption either; we keep such a channel
"watch" rather than returning it to the pool.

Exception, used once: one probe suffices when three conditions hold together — the
fingerprint's top prediction is a *specific other* model, that model is named, and the
probability for the claimed one is ≤ 0.1%. That combination is not noise.

## What we did not prove

The fingerprint bank covers a fixed list of reference models and is versioned; a model
younger than the bank scores as `unknown`, which is not the same as "fake". We treat it as
insufficient data and re-probe, never as a negative verdict.

Motion detection is heuristic: a drawing that animates via `requestAnimationFrame` at
sub-frame intervals can look static to a four-frame sampler. We cross-check with a DOM probe
before calling it motionless.

Cheap channels are cheap for a reason, and the reason is not always honest. That is the whole
argument for probing rather than trusting a listing.