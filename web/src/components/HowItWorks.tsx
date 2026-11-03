const STEPS = [
  ['Pin the source', 'Each answer is scoped to one pinned 10-K accession with content hashes. A changed upstream file fails loudly instead of silently mixing versions.'],
  ['Route by question', 'Numbers go to a deterministic calculator over SEC companyfacts; explanations go to retrieval over the filing text. The model never does the arithmetic.'],
  ['Select the period by dates', 'Fiscal periods come from fact start/end dates, never SEC fy/fp tags — Apple’s FY2023 comparative is tagged fy=2025.'],
  ['Mint evidence IDs', 'Every citable output gets a stable ID: ev_ for passages, fact_ for reported values, met_ for calculated metrics.'],
  ['Verify every claim', 'Cited IDs are reloaded and checked: existence, hash, ticker, single accession, then unit, period and value. A judge from another vendor checks narrative entailment only after that.'],
  ['Answer only what passed', 'The verified answer is built from supported claims only. Unsupported ones are listed, never stated. One bounded retry is allowed.'],
]

export function HowItWorks() {
  return (
    <div className="board">
      <div className="board-inner">
        <h1>Every number has a receipt.</h1>
        <p className="lede">
          A single tool-calling agent over eight typed tools. The model reads, plans and writes; deterministic code fetches, calculates and
          checks. This console replays recorded evaluation runs exactly as they happened — nothing is regenerated for the demo.
        </p>
        <div className="pipeline">
          {STEPS.map(([title, body], i) => (
            <div className="card pipe" key={title}>
              <span className="n">0{i + 1}</span>
              <strong>{title}</strong>
              <p>{body}</p>
            </div>
          ))}
        </div>

        <div className="section-title">How to read the console</div>
        <div className="card caveats">
          <ul>
            <li><b>A / B dots</b> on each question are the eval grade in the baseline and the render-v2 run.</li>
            <li><b>Click a claim</b> to see the verifier's checklist; <b>click an evidence chip</b> to see the exact passage, fact or calculation the agent cited.</li>
            <li>A <b>metric</b> (met_) opens its formula and inputs; each input opens the underlying SEC fact with its XBRL tag, period and accession.</li>
            <li><b>Compare A/B</b> puts both runs side by side — "Fixed by v2" filters to questions the change turned from wrong to right.</li>
          </ul>
        </div>
      </div>
    </div>
  )
}
