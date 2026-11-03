import { useState } from 'react'
import type { Step } from '../types'

export function Timeline({ steps }: { steps: Step[] }) {
  if (steps.length === 0) return <div className="refusal"><p>No tool calls were recorded.</p></div>
  return (
    <div className="timeline">
      {steps.map((s, i) => (
        <StepRow key={i} n={i + 1} step={s} />
      ))}
    </div>
  )
}

function StepRow({ n, step }: { n: number; step: Step }) {
  const [open, setOpen] = useState(false)
  const cls = !step.ok ? 'err' : step.name === 'verify_citations' ? 'verify' : ''
  return (
    <div className={`step ${cls}`}>
      <div className="step-num">{n}</div>
      <div className="step-body">
        <div className="step-head">
          <span className="step-name">{step.name}</span>
          <span className="step-summary">{step.summary}</span>
          <button className="step-toggle" onClick={() => setOpen(!open)}>{open ? 'hide args' : 'args'}</button>
        </div>
        {open && <pre className="args">{JSON.stringify(step.args, null, 2)}</pre>}
      </div>
    </div>
  )
}
