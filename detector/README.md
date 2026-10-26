# Embedded numerical fingerprint scoring

No server, original CLI, provider keys, network or runtime Node packages. Node 24.
Python owns provider requests; this runner receives reply text only.

## Interface

Invoke once per scoring operation, including from a different working directory:

```python
completed = subprocess.run(
    ["node", "--max-old-space-size=384", str(root / "detector" / "score.mjs")],
    input=json.dumps({"outputs": outputs, "target_model": target_id}),
    text=True, capture_output=True, timeout=15, check=True,
)
result = json.loads(completed.stdout)
```

`outputs`: zero to three `{text: str, expected_count: positive_int}` objects.
Exactly three accepted, model-like replies required for calibrated scoring.
Input maximum 256 KiB; each text maximum 64 KiB; counts maximum 10000.
Other fields are discarded, never scored. No API key is needed or inspected.

Output: compact JSON plus newline, exactly these fields:

```json
{"decision":"ranked","prediction":"model-id","prediction_name":"Display name","probability":0.5,"probability_status":"reference_calibrated","target_probability":0.5,"results":[{"model":"model-id","display_name":"Display name","probability":0.5,"score":1.2}],"used_outputs":3,"diagnostics":[],"reference_version":"4dcb4216151cbd5066d515297720130a9fd3b198","warning":"Relative probabilities within the reference bank only; not proof of model identity or probability against unknown models."}
```

`decision`: `ranked`, `not_model`, `unscorable`. `probability_status`:
`reference_calibrated` or `unavailable`. Probabilities are raw 0..1 closed-set
values, never percentages. Calibration absence/mismatch produces nulls, not
confidence inferred from scores. `target_probability` is null for an absent
**exact** target ID; no normalization, aliases, provider names, display names
or identity guesses. Supported `deepseek-v4.1-flash-1017` is distinct from
`deepseek-v4.1-flash`. Unsupported targets do not invalidate other candidates'
closed-set ranking; their `probability_status` can remain `reference_calibrated`.

Each diagnostic contains `index`, `parsed_numbers`, `minimum_numbers`,
`accepted`, `raw_out_of_range_count`. Original parser and acceptance rules:
longest integer run, range 1..355; minimum `max(80, ceil(expected_count*0.55))`.
Original sequence-shape checks may reject sorted, program-RNG or shuffled lists.
No partial ranking/confidence is enabled. Empty/invalid/insufficient replies
cannot establish identity.

Invalid input or artifact failures produce structured `unscorable` JSON,
nonzero exit status, one controlled error code on stderr; no exceptions,
provider secrets, reply text or input are logged. Valid stderr is empty.

## Catalog and provenance

`catalog.json` is Python-readable. Fields include `models` (exact `id`,
`display_name`, `family`), three `challenges` (`id`, `prompt`, `expected_count`),
`source_commit`, `reference_version`, `bank_built_at`, and acceptance constants.
Use the three challenge prompts unchanged, with an empty system prompt. Do
not include a guessed model identity instruction. These are original reference
challenge prompts, not newly invented templates.

`LICENSE`, `NOTICE`, `provenance.json` preserve MIT provenance, pinned upstream
commit, original file SHA-256 and git blob hashes. Four original scoring modules
are unmodified in `upstream/`; the original binary is unchanged. The bank omits
unused numeric data, already present in the ranker's binary.

## Rebuild (development only)

Install esbuild only outside this project; no project package installation:

```sh
npm install --prefix /tmp/lm-detector-build --no-audit --no-fund esbuild@0.25.12
/tmp/lm-detector-build/node_modules/.bin/esbuild detector/score.ts \
  --bundle --platform=node --format=esm --target=node24 \
  --outfile=detector/score.mjs --legal-comments=inline \
  --banner:js='// lm-detector MIT; upstream 4dcb4216151cbd5066d515297720130a9fd3b198. See LICENSE and NOTICE.'
```

The bundled runner has only Node builtin imports. No dependencies at runtime.

## Verification

`tests/test_detector_scoring.py` invokes the actual subprocess against three
original reference samples (`fixtures/reference_samples.json`) and compares all
63 model scores/probabilities with `fixtures/original_expected.json`, generated
from the original upstream shared modules plus full original bank. Exact target
probability: `0.9999930549015217` on Node v24.22.0. This is a self-reference
implementation-parity fixture, **not** held-out accuracy evidence.

Observed single invocation under `--max-old-space-size=384`: 0.42 s,
112656 KiB peak RSS; original full-bank run 0.53 s, 155780 KiB. Enforce a parent
subprocess timeout; avoid launching an unbounded number concurrently.

## Update 2026-10-25

Upstream 4dcb4216151cbd5066d515297720130a9fd3b198 adds gemini-3.5-flash-lite, kimi-k3.1-pro and minimax-m3.1-flash (63 references total). The float codec now uses byte-grouped float32 storage; source decoder and artifact are updated together. Sequence-shape admission follows upstream OR rule (reject only when both signals exceed reference limits). Challenge prompts, expected counts and minimum-number thresholds remain unchanged. Exact independent original full-bank and embedded slim-bank parity verified on Node v24.22.0.
