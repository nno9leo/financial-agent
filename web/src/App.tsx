import { useEffect, useMemo, useState } from 'react'
import type { Bundle } from './types'
import { buildQuestions, loadBundle } from './lib'
import { Console, type Arm } from './components/Console'
import { Scoreboard } from './components/Scoreboard'
import { HowItWorks } from './components/HowItWorks'

type View = 'console' | 'scoreboard' | 'how'

// Set when this console is hosted inside another site, so readers have a way back.
const homeUrl = import.meta.env.VITE_HOME_URL as string | undefined

export default function App() {
  const [bundle, setBundle] = useState<Bundle | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<View>('console')
  const [arm, setArm] = useState<Arm>('b')

  useEffect(() => {
    loadBundle().then(setBundle, (e) => setError(String(e)))
  }, [])
  const questions = useMemo(() => (bundle ? buildQuestions(bundle) : []), [bundle])

  if (error) return <div className="loading">Could not load replay data: {error}</div>
  if (!bundle) return <div className="loading">Loading recorded runs…</div>

  return (
    <div className="shell">
      <header className="topbar">
        {homeUrl && <a className="home-link" href={homeUrl}>Back to portfolio</a>}
        <div className="brand">
          <span className="brand-mark">✓</span>
          Audit Trail<small>SEC 10-K research agent · replay</small>
        </div>
        <nav className="nav">
          {([['console', 'Console'], ['scoreboard', 'Scoreboard'], ['how', 'How it works']] as const).map(([v, label]) => (
            <button key={v} className={view === v ? 'active' : ''} onClick={() => setView(v)}>{label}</button>
          ))}
        </nav>
        <div className="topbar-right">
          {view === 'console' && (
            <div className="seg" role="tablist" aria-label="Run">
              {([['a', 'A · Baseline'], ['b', 'B · Render v2'], ['ab', 'Compare A/B']] as const).map(([v, label]) => (
                <button key={v} className={arm === v ? 'active' : ''} onClick={() => setArm(v)}>{label}</button>
              ))}
            </div>
          )}
          <span className="pill-note">MSFT FY2026 · AAPL FY2025</span>
        </div>
      </header>
      {view === 'console' && <Console bundle={bundle} questions={questions} arm={arm} />}
      {view === 'scoreboard' && <Scoreboard bundle={bundle} />}
      {view === 'how' && <HowItWorks />}
    </div>
  )
}
