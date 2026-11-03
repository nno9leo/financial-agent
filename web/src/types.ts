// Shapes of web/public/data/console.json, produced by scripts/export_ui_data.py.

export type Verdict = 'supported' | 'partially_supported' | 'unsupported'

export interface Frac {
  num: number
  den: number
}

export interface Verification {
  claim_id: string
  claim_text: string
  claim_type: 'numeric' | 'narrative'
  evidence_ids: string[]
  verdict: Verdict
  reason: string | null
  semantic_score: number | null
  deterministic_checks: Record<string, boolean | string | null>
}

export interface Attempt {
  attempt: number
  is_final: boolean
  final_answer_markdown: string | null
  counts: { supported: number; partially_supported: number; unsupported: number }
  verifications: Verification[]
}

export interface Step {
  name: string
  args: Record<string, unknown>
  ok: boolean
  error_code: string | null
  summary: string
  evidence_ids: string[]
}

export interface Item {
  question_id: string
  question: string
  question_type: string
  split: 'dev' | 'holdout'
  ticker: string | null
  accession_number: string | null
  expected: Record<string, unknown> | null
  expected_tools: string[] | null
  final_text: string | null
  latency_s: number | null
  cost_usd: number
  tokens_in: number | null
  tokens_out: number | null
  runtime_error: string | null
  grades: {
    numeric_correct: boolean | null
    refusal_correct: boolean | null
    tool_selection_correct: boolean | null
    retrieval_hit_at_k: boolean | null
    retrieval_rank: number | null
    unsupported_claim_count: number | null
    unsupported_claim_n_claims: number | null
    retry_used: boolean | null
    error_categories: string[] | null
  }
  steps: Step[]
  attempts: Attempt[]
}

export interface Metrics {
  items: number
  p1_strict_numeric: Frac
  numeric_correct: Frac
  unsupported_claims: Frac
  refusal_correct: Frac
  tool_selection: Frac
  retrieval_hit: Frac
  latency_p50_s: number | null
  latency_p95_s: number | null
  cost_usd: number
  run_errors: Frac
}

export interface Run {
  run_id: string
  arm: 'A' | 'B'
  label: string
  split: string
  render: string
  agent_model: string | null
  agent_thinking: string | null
  judge_model: string | null
  git_sha: string | null
  regrade_history: string
  metrics: Metrics
  metrics_by_split: Record<'dev' | 'holdout', Metrics>
  items: Item[]
}

export interface ChunkEvidence {
  kind: 'chunk'
  text: string
  section: string | null
  chunk_id: string | null
  ticker: string | null
  accession_number: string | null
  source_url: string | null
  start_offset: number | null
  end_offset: number | null
  content_hash: string | null
  filing_date: string | null
}

export interface FactEvidence {
  kind: 'fact'
  label: string | null
  tag: string | null
  taxonomy: string | null
  value: string | null
  unit: string | null
  period_start: string | null
  period_end: string | null
  fiscal_year: number | null
  frame: string | null
  form: string | null
  filed_at: string | null
  accession_number: string | null
  ticker: string | null
  source_url: string | null
}

export interface MetricEvidence {
  kind: 'metric'
  metric: string | null
  formula: string | null
  value: string | null
  display_value: string | null
  unit: string | null
  period_start: string | null
  period_end: string | null
  inputs: string[]
  ticker: string | null
  accession_number: string | null
}

export type Evidence = ChunkEvidence | FactEvidence | MetricEvidence

export interface Bundle {
  dataset_sha256: string
  runs: Run[]
  evidence: Record<string, Evidence>
}

/** One question with its record in each arm (B merges the dev and holdout render-v2 runs). */
export interface Question {
  id: string
  question: string
  type: string
  split: 'dev' | 'holdout'
  ticker: string | null
  a: Item | undefined
  b: Item | undefined
}
