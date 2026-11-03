import type { Bundle, Frac, Metrics } from '../types'
import { REASON_LABEL, finalAttempt, fracText, pct, secs, usd } from '../lib'

function rate(f: Frac) {
  return f.den ? f.num / f.den : 0
}

function Tile({ label, from, to, lowerIsBetter, sub }: { label: string; from: Frac; to: Frac; lowerIsBetter?: boolean; sub: string }) {
  const better = lowerIsBetter ? rate(to) < rate(from) : rate(to) > rate(from)
  return (
    <div className="card tile">
      <div className="tile-label">{label}</div>
      <div className="tile-row">
        <span className="from">{pct(from)}</span>
        <span className="arrow">→</span>
        <span className={`to ${better ? 'good' : ''}`}>{pct(to)}</span>
      </div>
      <div className="bar"><i style={{ width: `${rate(from) * 100}%`, background: 'var(--line-strong)' }} /></div>
      <div className="bar"><i style={{ width: `${rate(to) * 100}%`, background: better ? 'var(--ok)' : 'var(--muted)' }} /></div>
      <div className="tile-sub">A {fracText(from)} → B {fracText(to)} · {sub}</div>
    </div>
  )
}

export function Scoreboard({ bundle }: { bundle: Bundle }) {
  const [base, dev, holdout] = bundle.runs
  const aDev = base.metrics_by_split.dev
  const aHold = base.metrics_by_split.holdout
  const reasons = new Map<string, number>()
  for (const item of base.items) {
    for (const v of finalAttempt(item)?.verifications ?? []) {
      if (v.verdict === 'unsupported' && v.reason) reasons.set(v.reason, (reasons.get(v.reason) ?? 0) + 1)
    }
  }
  const rows: [string, string, Metrics][] = [
    ['A · Baseline', 'dev', aDev],
    ['B · Render v2', 'dev', dev.metrics],
    ['A · Baseline', 'holdout', aHold],
    ['B · Render v2', 'holdout', holdout.metrics],
  ]
  return (
    <div className="board">
      <div className="board-inner">
        <h1>Measured, not asserted.</h1>
        <p className="lede">
          35 frozen questions, ground truth transcribed by hand from the filing (never from the pipeline being graded), and a
          pre-registered A/B on a single variable: how calculator results are rendered back to the agent. The holdout split was run once,
          after the decision thresholds were committed.
        </p>

        <div className="tiles">
          <Tile label="Strict numeric accuracy · dev" from={aDev.p1_strict_numeric} to={dev.metrics.p1_strict_numeric} sub="numeric + mixed" />
          <Tile label="Strict numeric accuracy · holdout" from={aHold.p1_strict_numeric} to={holdout.metrics.p1_strict_numeric} sub="numeric + mixed" />
          <Tile label="Unsupported claims · dev" from={aDev.unsupported_claims} to={dev.metrics.unsupported_claims} lowerIsBetter sub="of all claims" />
          <Tile label="Unsupported claims · holdout" from={aHold.unsupported_claims} to={holdout.metrics.unsupported_claims} lowerIsBetter sub="of all claims" />
        </div>

        <div className="section-title">Every rate with its denominator</div>
        <div className="card table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Arm</th><th>Split</th><th>Items</th><th>Strict numeric</th><th>Unsupported claims</th><th>Refusals</th>
                <th>Tool selection</th><th>Retrieval hit@5</th><th>p50 / p95</th><th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([arm, split, m]) => (
                <tr key={arm + split}>
                  <td>{arm}</td>
                  <td>{split}</td>
                  <td>{m.items}</td>
                  <td>{fracText(m.p1_strict_numeric)}</td>
                  <td>{fracText(m.unsupported_claims)}</td>
                  <td>{fracText(m.refusal_correct)}</td>
                  <td>{fracText(m.tool_selection)}</td>
                  <td>{fracText(m.retrieval_hit)}</td>
                  <td>{secs(m.latency_p50_s)} / {secs(m.latency_p95_s)}</td>
                  <td>{usd(m.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="section-title">Why baseline claims were rejected</div>
        <div className="card table-wrap">
          <table className="data">
            <thead><tr><th>Verifier reason</th><th>Final claims</th></tr></thead>
            <tbody>
              {[...reasons.entries()].sort((x, y) => y[1] - x[1]).map(([r, n]) => (
                <tr key={r}><td>{REASON_LABEL[r] ?? r}</td><td>{n}</td></tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="section-title">Read with care</div>
        <div className="card caveats">
          <ul>
            <li>One run per arm; the holdout has only 9 strict numeric items and 1 refusal item. This is limited evidence, not a guarantee.</li>
            <li>The rendering defect was diagnosed by reading baseline records for all items, holdout included, so the holdout is not perfectly untouched.</li>
            <li>The first dev result was rejected by its own side-effect guard; the refusal policy was then fixed in ADR 0011 and every run regraded offline without touching raw records.</li>
            <li>Each run includes one item that timed out before the idle-stall watchdog existed.</li>
            <li>The verifier keeps unsupported claims out of the verified answer — it makes them visible, it doesn't make the model produce fewer of them. The render fix did that.</li>
          </ul>
        </div>
        <p className="pill-note" style={{ marginTop: 18 }}>
          Dataset v1 · SHA-256 <span className="mono">{bundle.dataset_sha256}</span> · agent {base.agent_model} ({base.agent_thinking}) · judge {base.judge_model}
        </p>
      </div>
    </div>
  )
}
