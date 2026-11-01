# Parallel channel verification: multi-token lanes

How to turn serial channel probing into a parallel pipeline. `CHANNEL-PROBING-NOTES.md`
covers *what* to check per channel (fingerprint, prompt tokens, drawing, motion, ceiling);
this note covers *how to run many of them at once* without the probes interfering with
each other — and what that saves. Numbers below come from real batches on one reseller;
the method is what transfers, not the counts.

## The invariant that makes parallelism free

On OpenAI-compatible marketplaces, a client key's route for a model is a per-pair
property: **(token, model)**. Two probes collide only if they share BOTH the token and
the model. Everything follows from that:

- N parallel lanes on the **same model** need N **distinct tokens**;
- the **same token** can serve two lanes on **different models** simultaneously —
  the subscription writes never touch the same object;
- 10 lanes on 2 models need only 10 tokens: each key works in two pairs at once,
  `(key, model-A)` and `(key, model-B)`.

If you only have one API token, you cannot parallelize same-model probes without the
routes fighting over the token's subscription. Provision a small pool of throwaway
keys first — that pool is the whole parallelism budget.

## The lane pattern

One lane = one token = one queue of channels:

1. **Pin** the lane's `(token, model)` subscription to the channel being probed
   (usually: make it the highest-priority writable route).
2. **Probe** — run the full check battery through that token.
3. **Restore** the subscription to its baseline route set afterwards, so the token
   goes back to normal traffic.
4. **Next channel** in the queue.

The dangerous step is (3): a restore can fail (the marketplace may 400 with
"channel disabled or unavailable" while the pin is still in place). The lane must
never exit leaving the token pinned. Two guards:

- **kill-safe heal**: if the lane dies mid-probe, a supervising process must be able
  to re-run the restore independently of the dead lane — restore from a saved baseline
  snapshot, not from the dying process's memory;
- **restore filters the baseline against the live marketplace catalog** — channels
  that vanished get dropped on restore. Expect the restored route count to be a few
  short of the original; that is not a loss.

## Wall time is the max, not the sum

Serial: 20 channels x ~13 min = 4.2 hours. Parallel (10 tokens, 2 models, one wave of
5 lanes each): the wall time of a wave equals the **slowest channel in it**, not the sum.

| | serial batch (20 ch) | parallel batch (10 ch, 5+5 lanes) |
|---|---|---|
| wall time | 4.25 h | 0.81 h (25 + 23 min per wave) |
| per-channel wall | 12.75 min | 4.9 min |
| channels per wall-hour | 4.7 | 12.3 |
| spend per channel | $0.0043 | $0.0029–0.0039 |

Two properties worth internalizing:

- **Throughput scales with lanes, not with money.** Burning 5 subscriptions at once
  raises spend-per-wall-hour (~1.8x here), but the batch finishes 2.6x sooner — and
  each channel still costs less than serial because the slow channels stop blocking
  the fast ones.
- **A wave is as slow as its worst member.** One 25-minute channel defines the wave;
  four 6-minute channels finish and idle. Batching channels by expected latency
  (fast with fast) squeezes more waves into the same hour — but only if you already
  know the latencies; on first contact, mixed batches are fine.

## Verdicts need a retry policy, not just thresholds

Two verdict classes look identical from one run and are not:

- **A transport episode** — the request succeeded at the gateway, the stream
  "completed", but the content is empty (the marketplace had a blip, a stream got
  committed with no usage). The channel may be perfectly honest. **Always re-probe
  once before believing a `dead`/`insufficient_data` verdict.** A real re-verification
  batch: 1 of 10 channels looked dead on first contact; the retry drew cleanly and
  the fingerprint came back at 99.7%.
- **Real drift** — fingerprint probability for the claimed model collapsed, the
  local classifier names a *different* model, and the token counts stay in the honest
  corridor. One run is evidence, not a verdict: re-check on the next batch, and treat
  an unambiguous triple (wrong top prediction + named foreign model + probability
  below ~0.1%) as the only single-run exception.

Also budget for the retry: on some marketplaces an empty response is still billed
(`gateway_estimate` on the delivered stream). Small, but it means a re-probe costs
more than zero even when the first attempt produced nothing.

## Re-verification is not optional

A clean probe buys trust until the next probe. In the re-verification batch above,
channels that had passed a full battery days earlier came back serving a different
model — three out of five on one lane, including the highest-priority route in the
live pool. The cadence that worked: re-verify every channel feeding production traffic
whenever a batch of new channels is probed anyway. The marginal cost is cents, and the
alternative is production traffic silently routed to a model you never chose.

## Operator checklist

- [ ] N tokens provisioned, unique values verified (duplicates collapse lanes silently)
- [ ] baseline route snapshot saved per token before the first pin
- [ ] lane exit always runs restore; a supervisor can heal a dead lane from the snapshot
- [ ] restore drops vanished channels gracefully (route count may shrink — fine)
- [ ] `dead`/`insufficient_data` verdicts re-probed once before being recorded
- [ ] spend reconciled from the marketplace's own request log, not from price sheets
- [ ] re-verification scheduled for every production channel
