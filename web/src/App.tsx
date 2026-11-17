import { useEffect, useState } from 'react'
import { Activity, ClipboardCheck, FlaskConical, Layers, Network, Radar } from 'lucide-react'
import { useFetch } from './api'
import Coverage from './views/Coverage'
import Evals from './views/Evals'
import Explore from './views/Explore'
import GraphView from './views/GraphView'
import LiveRun, { RebuildButton } from './views/LiveRun'
import System from './views/System'

const TABS = [
  { key: 'system', label: 'System', icon: Layers, hint: 'what it is, what it saw' },
  { key: 'live', label: 'Live run', icon: Activity, hint: 'analyse a PR, live' },
  { key: 'graph', label: 'Blast radius', icon: Network, hint: 'the PR as a graph' },
  { key: 'coverage', label: 'Coverage', icon: ClipboardCheck, hint: 'UI × tests × spec' },
  { key: 'explore', label: 'Exploration agent', icon: Radar, hint: 'replay + guardrails' },
  { key: 'evals', label: 'Evals & cost', icon: FlaskConical, hint: 'is it right, every time?' },
] as const
type Tab = typeof TABS[number]['key']

export default function App() {
  const initial = (location.hash.slice(1) as Tab) || 'system'
  const [tab, setTab] = useState<Tab>(TABS.some(t => t.key === initial) ? initial : 'system')
  const [pr, setPr] = useState(311)
  const { data: overview, reload } = useFetch<any>('/api/overview')
  useEffect(() => { location.hash = tab }, [tab])
  const reports: number[] = overview?.reports?.length ? overview.reports : [311, 268]

  return (
    <div className="flex h-full">
      <aside className="w-64 shrink-0 border-r border-ink-800 bg-ink-950/60 backdrop-blur flex flex-col">
        <div className="px-5 py-6">
          <div className="flex items-center gap-2.5">
            <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-brand to-brand-2 shadow-lg shadow-brand/30">
              <Radar size={18} className="text-ink-950" />
            </div>
            <div>
              <div className="text-[15px] font-semibold leading-tight">Blast radius</div>
              <div className="text-[11px] text-ink-400">what could this PR break?</div>
            </div>
          </div>
        </div>
        <nav className="px-3 space-y-1">
          {TABS.map(t => {
            const Icon = t.icon
            const on = tab === t.key
            return (
              <button key={t.key} onClick={() => setTab(t.key)}
                className={`w-full flex items-center gap-3 rounded-xl px-3 py-2.5 text-left transition ${on ? 'bg-brand/15 text-ink-100' : 'text-ink-300 hover:bg-ink-850'}`}>
                <Icon size={17} className={on ? 'text-brand' : ''} />
                <span><span className="block text-sm font-medium">{t.label}</span><span className="block text-[11px] text-ink-400">{t.hint}</span></span>
              </button>
            )
          })}
        </nav>
        <div className="mt-auto p-5 space-y-3 text-[11px] text-ink-400">
          <div className="space-y-1">
            <div><span className="text-ink-300">App</span> · <a className="hover:text-brand" href={overview?.app_url} target="_blank">{overview?.app_url?.replace('https://', '')}</a></div>
            <div><span className="text-ink-300">Repo</span> · <a className="hover:text-brand" href={`https://github.com/${overview?.repo}`} target="_blank">{overview?.repo?.split('/')[1]}</a></div>
            <div><span className="text-ink-300">Model</span> · {overview?.model}</div>
          </div>
          <div onClick={() => setTimeout(reload, 4000)}><RebuildButton /></div>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto scroll-thin">
        <header className="sticky top-0 z-10 border-b border-ink-800 bg-ink-950/70 backdrop-blur px-8 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold">{TABS.find(t => t.key === tab)?.label}</h1>
            <p className="text-xs text-ink-400">Crawl + spec + tests + code in one Neo4j graph · agents on top, checked by code</p>
          </div>
          <div className="flex items-center gap-2 text-xs text-ink-400">
            <span className="h-2 w-2 rounded-full bg-ok" /> connected to the real pipeline
          </div>
        </header>
        <div className="px-8 py-6 max-w-[1800px]">
          {tab === 'system' && <System />}
          {tab === 'live' && <LiveRun initialPr={pr} onDone={(n) => { setPr(n); reload() }} />}
          {tab === 'graph' && <GraphView pr={pr} setPr={setPr} reports={reports} />}
          {tab === 'coverage' && <Coverage />}
          {tab === 'explore' && <Explore />}
          {tab === 'evals' && <Evals />}
        </div>
      </main>
    </div>
  )
}
