// Local transport adapter. Upstream numerical code is unchanged in ./upstream/.
import { readFile } from 'node:fs/promises'
import { analyzeSharedOutputs } from './upstream/shared-detector'
import { decodeDetector } from './upstream/detector-codec'

const SOURCE_COMMIT = '4dcb4216151cbd5066d515297720130a9fd3b198'
const MAX_INPUT_BYTES = 262144
const CLOSED_SET_WARNING = 'Relative probabilities within the reference bank only; not proof of model identity or probability against unknown models.'

function validate(input: unknown) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('invalid_input')
  const value = input as Record<string, unknown>
  if (!Array.isArray(value.outputs) || value.outputs.length > 3 || typeof value.target_model !== 'string' || value.target_model.length > 200) throw new Error('invalid_input')
  const outputs = value.outputs.map((output: unknown) => {
    if (!output || typeof output !== 'object' || Array.isArray(output)) throw new Error('invalid_input')
    const row = output as Record<string, unknown>
    if (typeof row.text !== 'string' || Buffer.byteLength(row.text) > 65536 || !Number.isInteger(row.expected_count) || (row.expected_count as number) < 1 || (row.expected_count as number) > 10000) throw new Error('invalid_input')
    // Deliberately discard request/provider metadata: only numerical reply text is scored.
    return { text: row.text, expected_count: row.expected_count as number }
  })
  return { outputs, target_model: value.target_model }
}

async function main() {
  let size = 0
  const chunks: Buffer[] = []
  for await (const chunk of process.stdin) {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    size += data.length
    if (size > MAX_INPUT_BYTES) throw new Error('invalid_input')
    chunks.push(data)
  }
  let raw: unknown
  try { raw = JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch { throw new Error('invalid_input') }
  const input = validate(raw)
  const [bankData, artifactData] = await Promise.all([
    readFile(new URL('./bank.json', import.meta.url), 'utf8'),
    readFile(new URL('./shared_detector.bin', import.meta.url)),
  ])
  const bank = JSON.parse(bankData)
  const artifact = decodeDetector(artifactData)
  const analysis = analyzeSharedOutputs(input.outputs, bank, artifact)
  const supported = bank.models.some((model: {id: string}) => model.id === input.target_model)
  const target = supported ? analysis.results.find(result => result.model === input.target_model) : undefined
  const warnings = [CLOSED_SET_WARNING]
  if (!supported) warnings.push('unsupported_target: exact target model ID is absent from the reference bank; target_probability is unavailable. No aliases are inferred.')
  if (analysis.probability_status !== 'reference_calibrated') warnings.push('Calibrated probability unavailable: requires three valid model-like replies and matching calibration parameters.')
  process.stdout.write(JSON.stringify({
    decision: analysis.decision ?? 'ranked',
    prediction: analysis.prediction,
    prediction_name: analysis.prediction_name,
    probability: analysis.probability,
    probability_status: analysis.probability_status,
    target_probability: target?.probability ?? null,
    results: analysis.results.map(({model, display_name, probability, score}) => ({model, display_name, probability, score})),
    used_outputs: analysis.used_outputs,
    diagnostics: analysis.diagnostics,
    reference_version: SOURCE_COMMIT,
    warning: warnings.join(' '),
  }) + '\n')
}

main().catch(error => {
  // Never print exception messages, input, keys, reply text, stack traces, or provider details.
  const code = error instanceof Error && error.message === 'invalid_input' ? 'detector_invalid_input' : 'detector_scoring_failed'
  process.stderr.write(code + '\n')
  process.stdout.write(JSON.stringify({
    decision: 'unscorable', prediction: '', prediction_name: '', probability: null,
    probability_status: 'unavailable', target_probability: null, results: [],
    used_outputs: 0, diagnostics: [], reference_version: SOURCE_COMMIT, warning: code,
  }) + '\n')
  process.exitCode = 1
})
