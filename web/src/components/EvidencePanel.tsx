import type { Bundle, Evidence, Verification } from '../types'
import { CHECK_ORDER, VERDICT_LABEL, evidenceKind, evidenceTitle, formatValue, sectionLabel } from '../lib'

interface Props {
  bundle: Bundle
  claim?: Verification
  evidenceId?: string
  onEvidence: (id: string) => void
  onClose: () => void
}

/** Right-hand drawer: the cited record exactly as the agent saw it, and the checks run against it. */
export function EvidencePanel({ bundle, claim, evidenceId, onEvidence, onClose }: Props) {
  const ev = evidenceId ? bundle.evidence[evidenceId] : undefined
  return (
    <aside className="panel">
      <div className="panel-head">
        {evidenceId && <span className={`ev-chip ${evidenceKind(evidenceId)}`}>{evidenceKind(evidenceId)}</span>}
        <h3>{evidenceId ? evidenceTitle(ev) : 'Verification'}</h3>
        <button className="icon-btn" onClick={onClose} aria-label="Close panel">✕</button>
      </div>
      <div className="panel-body">
        {claim && <Checks claim={claim} />}
        {claim && claim.evidence_ids.length > 1 && (
          <div>
            <h4>Cited evidence</h4>
            <div className="chips">
              {claim.evidence_ids.map((id) => (
                <button key={id} className={`ev-chip ${evidenceKind(id)} ${id === evidenceId ? 'active' : ''}`} onClick={() => onEvidence(id)}>
                  {id}
                </button>
              ))}
            </div>
          </div>
        )}
        {evidenceId && !ev && <div className="empty-panel">{evidenceId} was cited but never returned by a tool in this run.</div>}
        {ev && <EvidenceBody id={evidenceId!} ev={ev} bundle={bundle} onEvidence={onEvidence} />}
      </div>
    </aside>
  )
}

function Checks({ claim }: { claim: Verification }) {
  const checks = claim.deterministic_checks ?? {}
  const rows = CHECK_ORDER.filter(([k]) => typeof checks[k] === 'boolean')
  const judged = claim.claim_type === 'narrative' && claim.semantic_score !== null
  return (
    <div>
      <h4>
        Verdict · <span className={`verdict ${claim.verdict}`}>{VERDICT_LABEL[claim.verdict]}</span>
      </h4>
      <ul className="checks">
        {rows.map(([k, label]) => {
          const ok = checks[k] === true
          let detail = ''
          if (k === 'value_matches' && checks.candidate_value) {
            detail = `|Δ| ${Number(checks.absolute_difference).toExponential(1)} ≤ ${Number(checks.tolerance).toExponential(0)}`
          }
          return (
            <li key={k} className={ok ? 't' : 'f'}>
              <span className={`check-icon ${ok ? 't' : 'f'}`}>{ok ? '✓' : '✕'}</span>
              {label}
              {detail && <span className="check-detail">{detail}</span>}
            </li>
          )
        })}
        {judged && (
          <li className={claim.verdict === 'supported' ? 't' : 'f'}>
            <span className={`check-icon ${claim.verdict === 'supported' ? 't' : 'f'}`}>{claim.verdict === 'supported' ? '✓' : '~'}</span>
            Semantic entailment (judge)
            <span className="check-detail">score {claim.semantic_score!.toFixed(2)}</span>
          </li>
        )}
      </ul>
      {judged && claim.reason && <div className="judge" style={{ marginTop: 10 }}>{claim.reason}</div>}
    </div>
  )
}

function EvidenceBody({ id, ev, bundle, onEvidence }: { id: string; ev: Evidence; bundle: Bundle; onEvidence: (id: string) => void }) {
  if (ev.kind === 'chunk') {
    return (
      <>
        <div>
          <h4>Passage retrieved by the agent</h4>
          <div className="source-text">{ev.text}</div>
        </div>
        <dl className="kv">
          <dt>Evidence ID</dt><dd>{id}</dd>
          <dt>Section</dt><dd>{sectionLabel(ev.section)}</dd>
          <dt>Chunk</dt><dd>{ev.chunk_id}</dd>
          <dt>Offsets</dt><dd>{ev.start_offset}–{ev.end_offset}</dd>
          <dt>Accession</dt><dd>{ev.accession_number}</dd>
          <dt>Content SHA-256</dt><dd>{ev.content_hash}</dd>
          <dt>Source</dt><dd>{ev.source_url && <a href={ev.source_url} target="_blank" rel="noreferrer">SEC EDGAR ↗</a>}</dd>
        </dl>
      </>
    )
  }
  if (ev.kind === 'fact') {
    return (
      <>
        <div>
          <h4>Reported value · SEC companyfacts</h4>
          <div className="big-value">{formatValue(ev.value, ev.unit)}</div>
          <div className="tile-sub" style={{ marginTop: 6 }}>{ev.value} {ev.unit}</div>
        </div>
        <dl className="kv">
          <dt>Evidence ID</dt><dd>{id}</dd>
          <dt>XBRL tag</dt><dd>{ev.taxonomy}:{ev.tag}</dd>
          <dt>Period</dt><dd>{ev.period_start ? `${ev.period_start} → ` : 'as of '}{ev.period_end}</dd>
          <dt>Frame</dt><dd>{ev.frame ?? '—'}</dd>
          <dt>Form / filed</dt><dd>{ev.form} · {ev.filed_at}</dd>
          <dt>Accession</dt><dd>{ev.accession_number}</dd>
          <dt>Source</dt><dd>{ev.source_url && <a href={ev.source_url} target="_blank" rel="noreferrer">SEC EDGAR ↗</a>}</dd>
        </dl>
      </>
    )
  }
  return (
    <>
      <div>
        <h4>Deterministic calculation</h4>
        <div className="big-value">{ev.display_value}</div>
        <div className="formula" style={{ marginTop: 8 }}>{ev.formula}</div>
        <div className="tile-sub" style={{ marginTop: 4 }}>exact: {ev.value} ({ev.unit})</div>
      </div>
      {ev.inputs.length > 0 && (
        <div>
          <h4>Inputs (click to inspect)</h4>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {ev.inputs.map((fid) => {
              const f = bundle.evidence[fid]
              return (
                <button key={fid} className="input-row" onClick={() => onEvidence(fid)}>
                  <span>{f?.kind === 'fact' ? f.label : fid}</span>
                  <span className="mono">{f?.kind === 'fact' ? formatValue(f.value, f.unit) : ''}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}
      <dl className="kv">
        <dt>Evidence ID</dt><dd>{id}</dd>
        <dt>Period</dt><dd>{ev.period_start ? `${ev.period_start} → ` : 'as of '}{ev.period_end}</dd>
        <dt>Ticker</dt><dd>{ev.ticker}</dd>
        <dt>Accession</dt><dd>{ev.accession_number}</dd>
      </dl>
    </>
  )
}
