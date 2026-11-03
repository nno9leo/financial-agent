import { useEffect, useMemo, useState } from 'react'
import Markdown from 'react-markdown'
import type { Bundle, Item, Question } from '../types'
import { TYPE_LABEL, expectedText, finalAttempt, outcome, secs, usd } from '../lib'
import { Claims, type Selection } from './Claims'
import { EvidencePanel } from './EvidencePanel'
import { Timeline } from './Timeline'

export type Arm = 'a' | 'b' | 'ab'

interface Props {
  bundle: Bundle
  questions: Question[]
  arm: Arm
}

type Filter = 'all' | 'improved' | 'numeric' | 'narrative' | 'refuse'

const FILTERS: [Filter, string][] = [
  ['all', 'All'],
  ['improved', 'Fixed by v2'],
  ['numeric', 'Numbers'],
  ['narrative', 'Narrative'],
  ['refuse', 'Should refuse'],
]

function matches(q: Question, f: Filter): boolean {
  if (f === 'improved') return outcome(q.a) === 'fail' && outcome(q.b) === 'pass'
  if (f === 'numeric') return ['numeric_lookup', 'comparison', 'mixed'].includes(q.type)
  if (f === 'narrative') return ['narrative', 'metadata'].includes(q.type)
  if (f === 'refuse') return q.type.includes('failure')
  return true
}

/** Default to a question that shows the verifier and the retry doing real work. */
function featured(questions: Question[]): string {
  const hit = questions.find((q) => outcome(q.a) === 'fail' && outcome(q.b) === 'pass' && (q.b?.attempts.length ?? 0) > 1)
  return (hit ?? questions[0]).id
}

export function Console({ bundle, questions, arm }: Props) {
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [activeId, setActiveId] = useState(() => featured(questions))
  const [selection, setSelection] = useState<Selection>({})

  const visible = useMemo(
    () => questions.filter((q) => matches(q, filter) && (!query || `${q.question} ${q.id}`.toLowerCase().includes(query.toLowerCase()))),
    [questions, filter, query],
  )
  const active = questions.find((q) => q.id === activeId) ?? questions[0]

  useEffect(() => setSelection({}), [activeId, arm])

  const groups = useMemo(() => {
    const out: [string, Question[]][] = []
    for (const q of visible) {
      const label = TYPE_LABEL[q.type] ?? q.type
      const last = out[out.length - 1]
      if (last && last[0] === label) last[1].push(q)
      else out.push([label, [q]])
    }
    return out
  }, [visible])

  const panelOpen = Boolean(selection.claim || selection.evidenceId)

  return (
    <div className={`console ${panelOpen ? 'with-panel' : ''}`}>
      <aside className="sidebar">
        <div className="sidebar-head">
          <input className="search" placeholder="Search 35 questions…" value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="chips">
            {FILTERS.map(([f, label]) => (
              <button key={f} className={`chip ${filter === f ? 'active' : ''}`} onClick={() => setFilter(f)}>
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="qlist">
          {groups.map(([label, qs]) => (
            <div key={label}>
              <div className="qgroup">{label}</div>
              {qs.map((q) => (
                <button key={q.id} className={`qrow ${q.id === active.id ? 'active' : ''}`} onClick={() => setActiveId(q.id)}>
                  <span className="qrow-text">{q.question}</span>
                  <span className="ab-dots" title="Outcome in baseline (A) and render v2 (B)">
                    <span className={`dot ${outcome(q.a)}`}>A</span>
                    <span className={`dot ${outcome(q.b)}`}>B</span>
                  </span>
                  <span className="qrow-meta">
                    {q.ticker && <span className="ticker">{q.ticker}</span>}
                    <span>{q.split}</span>
                  </span>
                </button>
              ))}
            </div>
          ))}
          {visible.length === 0 && <div className="empty-panel">No questions match.</div>}
        </div>
      </aside>

      <main className="main">
        <div className="main-inner">
          <QuestionHeader q={active} />
          {arm === 'ab' ? (
            <div className="compare">
              {(['a', 'b'] as const).map((side) => (
                <div className="compare-col" key={side}>
                  <div className="compare-head">
                    <h3>{side === 'a' ? 'A · Baseline' : 'B · Render v2'}</h3>
                    <span className={`dot ${outcome(active[side])}`} style={{ width: 'auto', padding: '0 6px' }}>
                      {outcome(active[side]) === 'pass' ? 'pass' : outcome(active[side]) === 'fail' ? 'fail' : '—'}
                    </span>
                  </div>
                  {active[side] ? (
                    <>
                      <Stats item={active[side]!} compact />
                      <Claims key={active.id} item={active[side]!} selection={selection} onSelect={setSelection} />
                    </>
                  ) : (
                    <div className="refusal"><p>Not in this run.</p></div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <Record item={active[arm]} selection={selection} onSelect={setSelection} />
          )}
        </div>
      </main>

      {panelOpen && (
        <EvidencePanel
          bundle={bundle}
          claim={selection.claim}
          evidenceId={selection.evidenceId}
          onEvidence={(id) => setSelection((s) => ({ ...s, evidenceId: id }))}
          onClose={() => setSelection({})}
        />
      )}
    </div>
  )
}

function QuestionHeader({ q }: { q: Question }) {
  const exp = expectedText(q.a)
  return (
    <div className="q-head">
      <div className="q-meta">
        {q.ticker && <span className="ticker">{q.ticker}</span>}
        <span>{TYPE_LABEL[q.type] ?? q.type}</span>·<span>{q.split} split</span>·
        <span className="mono">{q.id}</span>
        {q.a?.accession_number && <>·<span className="mono">{q.a.accession_number}</span></>}
      </div>
      <h1 className="q-title">{q.question}</h1>
      {exp && (
        <div className="expected">
          <b>Ground truth</b>
          <span>{exp}</span>
        </div>
      )}
    </div>
  )
}

function Stats({ item, compact }: { item: Item; compact?: boolean }) {
  const final = finalAttempt(item)
  const o = outcome(item)
  return (
    <div className="stats">
      <div className={`stat ${o === 'pass' ? 'good' : o === 'fail' ? 'bad' : ''}`}>
        <span>Graded</span>
        <strong>{o === 'pass' ? 'correct' : o === 'fail' ? 'incorrect' : 'n/a'}</strong>
      </div>
      {final && (
        <div className="stat">
          <span>Claims verified</span>
          <strong>
            {final.counts.supported + final.counts.partially_supported}/{final.verifications.length}
          </strong>
        </div>
      )}
      <div className="stat">
        <span>Tool calls</span>
        <strong>{item.steps.length}</strong>
      </div>
      {!compact && (
        <>
          <div className="stat">
            <span>Latency</span>
            <strong>{secs(item.latency_s)}</strong>
          </div>
          <div className="stat">
            <span>Cost (agent + judge)</span>
            <strong>{usd(item.cost_usd)}</strong>
          </div>
        </>
      )}
    </div>
  )
}

function Record({ item, selection, onSelect }: { item: Item | undefined; selection: Selection; onSelect: (s: Selection) => void }) {
  const [showProse, setShowProse] = useState(false)
  useEffect(() => setShowProse(false), [item])
  if (!item) return <div className="refusal"><p>This question was not part of this run.</p></div>
  const final = finalAttempt(item)
  return (
    <>
      <Stats item={item} />
      <div className="section-title">Verified answer</div>
      <Claims key={item.question_id} item={item} selection={selection} onSelect={onSelect} />

      {item.final_text && (
        <>
          <div className="section-title">Agent's own prose</div>
          <div className="card">
            <div className="prose-label">
              <span>Shown for transparency — only the claims above passed verification{final ? '' : ' (no claims were submitted)'}.</span>
              <button className="step-toggle" onClick={() => setShowProse(!showProse)}>{showProse ? 'collapse' : 'expand'}</button>
            </div>
            {showProse && (
              <div className="prose">
                <Markdown>{item.final_text}</Markdown>
              </div>
            )}
          </div>
        </>
      )}

      <div className="section-title">Tool trace · {item.steps.length} calls</div>
      <Timeline steps={item.steps} />
    </>
  )
}
