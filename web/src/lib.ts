import type { Bundle, Evidence, Frac, Item, Question, Verdict } from './types'

export async function loadBundle(): Promise<Bundle> {
  const res = await fetch(`${import.meta.env.BASE_URL}data/console.json`)
  if (!res.ok) throw new Error(`console.json: HTTP ${res.status}`)
  return res.json()
}

const TYPE_ORDER = ['numeric_lookup', 'comparison', 'mixed', 'narrative', 'metadata', 'failure', 'numeric_failure']

export function buildQuestions(bundle: Bundle): Question[] {
  const a = new Map<string, Item>()
  const b = new Map<string, Item>()
  for (const run of bundle.runs) {
    for (const item of run.items) (run.arm === 'A' ? a : b).set(item.question_id, item)
  }
  return [...a.values()]
    .map((item) => ({
      id: item.question_id,
      question: item.question,
      type: item.question_type,
      split: item.split,
      ticker: item.ticker,
      a: item,
      b: b.get(item.question_id),
    }))
    .sort((x, y) => TYPE_ORDER.indexOf(x.type) - TYPE_ORDER.indexOf(y.type) || x.id.localeCompare(y.id))
}

export const TYPE_LABEL: Record<string, string> = {
  numeric_lookup: 'Numeric',
  comparison: 'Comparison',
  mixed: 'Mixed',
  narrative: 'Narrative',
  metadata: 'Metadata',
  failure: 'Should refuse',
  numeric_failure: 'Should refuse',
}

export type Outcome = 'pass' | 'fail' | 'na'

/** The single grade that matters most for this question type, as the eval harness scored it. */
export function outcome(item: Item | undefined): Outcome {
  if (!item) return 'na'
  const g = item.grades
  if (item.runtime_error) return 'fail'
  if (g.refusal_correct !== null && g.refusal_correct !== undefined) return g.refusal_correct ? 'pass' : 'fail'
  if (g.numeric_correct !== null && g.numeric_correct !== undefined) return g.numeric_correct ? 'pass' : 'fail'
  if (g.unsupported_claim_n_claims) return g.unsupported_claim_count ? 'fail' : 'pass'
  return 'na'
}

export function finalAttempt(item: Item | undefined) {
  if (!item || item.attempts.length === 0) return undefined
  return item.attempts.find((a) => a.is_final) ?? item.attempts[item.attempts.length - 1]
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  supported: 'Supported',
  partially_supported: 'Partial',
  unsupported: 'Unsupported',
}

export const REASON_LABEL: Record<string, string> = {
  deterministic_match: 'Exact match against structured evidence',
  unit_mismatch: 'Unit mismatch — claim unit differs from the evidence unit',
  value_mismatch: 'Value mismatch — outside tolerance of the evidence value',
  period_mismatch: 'Period mismatch — evidence covers a different fiscal period',
  numeric_claim_without_structured_evidence: 'Numeric claim cited only text — needs a fact_ or met_ ID',
  no_evidence: 'No evidence ID cited',
}

/** Deterministic checks in the order the verifier runs them. */
export const CHECK_ORDER: [string, string][] = [
  ['evidence_ids_present', 'Evidence IDs present'],
  ['evidence_exists', 'Evidence exists in ledger'],
  ['hash_matches', 'Content hash matches'],
  ['provenance_consistent', 'Provenance consistent'],
  ['ticker_matches', 'Ticker matches'],
  ['single_accession', 'Single pinned accession'],
  ['text_evidence_present', 'Text evidence present'],
  ['structured_evidence_present', 'Structured evidence present'],
  ['unit_matches', 'Unit matches'],
  ['period_matches', 'Period matches'],
  ['value_matches', 'Value within tolerance'],
]

export function evidenceKind(id: string): 'chunk' | 'fact' | 'metric' {
  return id.startsWith('ev_') ? 'chunk' : id.startsWith('fact_') ? 'fact' : 'metric'
}

export function formatValue(value: string | null | undefined, unit: string | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return value
  if (unit === 'USD') {
    const abs = Math.abs(n)
    if (abs >= 1e9) return `$${(n / 1e9).toLocaleString('en-US', { maximumFractionDigits: 3 })}B`
    if (abs >= 1e6) return `$${(n / 1e6).toLocaleString('en-US', { maximumFractionDigits: 1 })}M`
    return `$${n.toLocaleString('en-US')}`
  }
  if (unit === 'ratio') return `${(n * 100).toFixed(2)}%`
  if (unit === 'USD/shares') return `$${n.toFixed(2)} / share`
  return `${n.toLocaleString('en-US', { maximumFractionDigits: 4 })}${unit ? ` ${unit}` : ''}`
}

export function evidenceTitle(ev: Evidence | undefined): string {
  if (!ev) return 'Unknown evidence'
  if (ev.kind === 'chunk') return `${ev.ticker ?? ''} · ${sectionLabel(ev.section)}`
  if (ev.kind === 'fact') return ev.label ?? ev.tag ?? 'Fact'
  return (ev.metric ?? 'metric').replace(/_/g, ' ')
}

export function sectionLabel(s: string | null | undefined): string {
  const map: Record<string, string> = {
    mdna: 'MD&A',
    risk_factors: 'Risk factors',
    business: 'Business',
    financial_statements: 'Financial statements',
  }
  return s ? (map[s] ?? s.replace(/_/g, ' ')) : 'Filing text'
}

export function pct(f: Frac): string {
  return f.den === 0 ? '—' : `${((100 * f.num) / f.den).toFixed(1)}%`
}

export function fracText(f: Frac): string {
  return `${f.num}/${f.den}`
}

export function secs(s: number | null | undefined): string {
  return s === null || s === undefined ? '—' : `${s.toFixed(1)}s`
}

export function usd(n: number): string {
  return `$${n.toFixed(n < 0.1 ? 4 : 2)}`
}

interface ExpectedValue {
  value?: Parameters<typeof formatValue>[0]
  unit?: Parameters<typeof formatValue>[1]
  display_unit?: string
}

interface ExpectedAnswer extends ExpectedValue {
  kind?: string
  expected_error_code?: string
  fields?: Record<string, unknown>
  numeric?: ExpectedValue
  values?: ExpectedValue[]
  key_points?: string[]
}

/** Compact expected-answer text for the question header. */
export function expectedText(item: Item | undefined): string | null {
  const exp = item?.expected as ExpectedAnswer | null | undefined
  if (!exp) return null
  if (exp.kind === 'refusal') return `Refuse · expected error ${exp.expected_error_code ?? 'any'}`
  if (exp.kind === 'metadata') return Object.values(exp.fields ?? {}).join(' · ')
  const one = (v: ExpectedValue) => {
    if (!v.unit) return null
    if (v.display_unit === 'multiple') return `${Number(v.value).toFixed(2)}x`
    return formatValue(v.value, v.unit)
  }
  if (exp.numeric) return one(exp.numeric)
  if (Array.isArray(exp.values)) return exp.values.map(one).filter(Boolean).join(' → ')
  if (exp.value !== undefined) return one(exp)
  if (Array.isArray(exp.key_points)) return `Key points: ${exp.key_points.join('; ')}`
  return null
}
