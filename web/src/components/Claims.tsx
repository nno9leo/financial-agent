import { useState } from 'react'
import type { Item, Verification } from '../types'
import { REASON_LABEL, VERDICT_LABEL, evidenceKind, finalAttempt } from '../lib'

export interface Selection {
  claim?: Verification
  evidenceId?: string
}

interface Props {
  item: Item
  selection: Selection
  onSelect: (s: Selection) => void
}

/** Final verified claims for one record, plus the rejected first attempt when a retry happened. */
export function Claims({ item, selection, onSelect }: Props) {
  const [showRejected, setShowRejected] = useState(false)
  const final = finalAttempt(item)
  if (!final) return <Refusal item={item} />
  const rejected = item.attempts.length > 1 ? item.attempts[0] : undefined
  const errors = [...new Set(item.steps.filter((s) => !s.ok).map((s) => s.error_code))]
  return (
    <div className="claims">
      {item.question_type.includes('failure') && (
        <div className="refusal">
          <strong>
            Requested figure {item.grades.refusal_correct ? 'declined' : 'not declined'}
            {errors.length > 0 && <span className="claim-type"> · tools returned {errors.join(', ')}</span>}
          </strong>
          <p>
            The pinned filing can't support the requested number. Under the refusal policy (ADR 0011) the agent may still state
            verified figures for other metrics or periods — those are the claims below.
          </p>
        </div>
      )}
      {rejected && rejected !== final && (
        <div className="attempt-note">
          <strong>↻</strong>
          <div>
            <strong>Attempt {rejected.attempt} rejected</strong> — {rejected.counts.unsupported} of{' '}
            {rejected.verifications.length} claim(s) failed verification (
            {[...new Set(rejected.verifications.filter((v) => v.verdict === 'unsupported').map((v) => v.reason))].join(', ')}
            ). The agent got one bounded retry and resubmitted.{' '}
            <button className="step-toggle" onClick={() => setShowRejected(!showRejected)}>
              {showRejected ? 'hide rejected claims' : 'show rejected claims'}
            </button>
          </div>
        </div>
      )}
      {rejected && rejected !== final && showRejected &&
        rejected.verifications.map((v) => (
          <ClaimCard key={`r-${v.claim_id}`} v={v} selection={selection} onSelect={onSelect} />
        ))}
      {rejected && rejected !== final && showRejected && <div className="section-title">Attempt {final.attempt} · final</div>}
      {final.verifications.map((v) => (
        <ClaimCard key={v.claim_id} v={v} selection={selection} onSelect={onSelect} />
      ))}
    </div>
  )
}

export function ClaimCard({ v, selection, onSelect }: { v: Verification; selection: Selection; onSelect: (s: Selection) => void }) {
  const selected = selection.claim === v
  const reason = v.reason ? (REASON_LABEL[v.reason] ?? v.reason) : null
  return (
    <div
      className={`card claim ${selected ? 'selected' : ''} ${v.verdict === 'unsupported' ? 'unsupported-claim' : ''}`}
      onClick={() => onSelect({ claim: v, evidenceId: v.evidence_ids[0] })}
    >
      <span className={`verdict ${v.verdict}`}>{VERDICT_LABEL[v.verdict]}</span>
      <div className="claim-text">{v.claim_text}</div>
      {reason && v.reason !== 'deterministic_match' && (
        <div className={`claim-reason ${v.verdict === 'unsupported' ? 'bad' : ''}`}>
          {v.semantic_score !== null && <span className="mono">judge {v.semantic_score.toFixed(2)} · </span>}
          {reason}
        </div>
      )}
      <div className="claim-foot">
        {v.evidence_ids.map((id) => (
          <button
            key={id}
            className={`ev-chip ${evidenceKind(id)} ${selected && selection.evidenceId === id ? 'active' : ''}`}
            onClick={(e) => {
              e.stopPropagation()
              onSelect({ claim: v, evidenceId: id })
            }}
          >
            {id}
          </button>
        ))}
        <span className="claim-type">{v.claim_type}</span>
      </div>
    </div>
  )
}

function Refusal({ item }: { item: Item }) {
  const errors = item.steps.filter((s) => !s.ok).map((s) => s.error_code)
  const refused = item.grades.refusal_correct
  return (
    <div className="refusal">
      <strong>{item.runtime_error ? 'Run did not complete' : 'No claims submitted for verification'}</strong>
      <p>
        {item.runtime_error
          ? item.runtime_error
          : errors.length
            ? `Tools returned typed errors (${[...new Set(errors)].join(', ')}), so the agent declined instead of inventing a number.`
            : 'The agent answered without calling verify_citations.'}
      </p>
      {refused !== null && (
        <p>
          Graded as <span className={refused ? 'improved' : ''}>{refused ? 'a correct refusal' : 'an incorrect refusal'}</span>.
        </p>
      )}
    </div>
  )
}
