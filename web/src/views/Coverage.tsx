import { useMemo, useState } from 'react'
import { ClipboardCheck, Search } from 'lucide-react'
import { useFetch } from '../api'
import { Badge, Card, Empty, Pill, Stat } from '../ui'

type Req = {
  id: string; statement: string; source: string; needs: string; kind: string; pages: string[]
  ui: 'covered' | 'weak' | 'unknown' | 'absent' | 'partial'; ui_confidence: number; gap_reason: string | null; partial: string | null
  tests: number; test_ids: string[]
}
const UI_COLOR: Record<string, string> = { covered: '#34d399', weak: '#a3e635', unknown: '#fbbf24', absent: '#f87171', partial: '#fb923c' }

export default function Coverage() {
  const { data, error } = useFetch<{ requirements: Req[]; summary: any }>('/api/coverage')
  const [filter, setFilter] = useState<'all' | 'unknown' | 'untested' | 'drift'>('all')
  const [q, setQ] = useState('')
  const rows = useMemo(() => (data?.requirements ?? []).filter(r =>
    (filter === 'all' || (filter === 'unknown' && r.ui !== 'covered') || (filter === 'untested' && !r.tests) || (filter === 'drift' && r.partial))
    && (!q || r.statement.toLowerCase().includes(q.toLowerCase()))), [data, filter, q])
  if (error) return <Empty>{error}</Empty>
  if (!data) return <div className="text-sm text-ink-400">Loading…</div>
  const s = data.summary
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <Stat label="Requirements" value={s.requirements} sub="spec tables (code) + prose (LLM)" />
        <Stat label="Shown in the UI" value={s.covered} accent="#34d399" sub={`real evidence on a screen · +${s.covered_weak_class_only ?? 0} weak (CSS class only)`} />
        <Stat label="Unknown" value={s.unknown} accent="#fbbf24" sub="needs a state we didn't reach, with the reason" />
        <Stat label="Checked by a test" value={s.requirements_tested} accent="#7c9cff" sub={`${s.tests} tests parsed, ${s.tests_linked} linked`} />
        <Stat label="No test at all" value={s.requirements_untested} accent="#f87171" sub="the list a Coverage agent hands to Gen" />
      </div>
      <Card title="Requirement × UI × tests" icon={<ClipboardCheck size={14} />}
        right={<div className="flex items-center gap-2">
          {(['all', 'unknown', 'untested', 'drift'] as const).map(f => <Pill key={f} active={filter === f} onClick={() => setFilter(f)}>{f === 'drift' ? 'spec drift' : f === 'unknown' ? 'not shown' : f}</Pill>)}
          <div className="relative ml-2"><Search size={13} className="absolute left-2.5 top-2 text-ink-400" />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="search" className="w-48 rounded-full bg-ink-800 pl-8 pr-3 py-1 text-xs outline-none focus:ring-1 ring-brand" /></div>
        </div>}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="text-left text-[11px] uppercase tracking-wider text-ink-400">
              <th className="py-2 pr-3 font-medium">Requirement</th><th className="py-2 pr-3 font-medium">Needs</th>
              <th className="py-2 pr-3 font-medium">In the UI</th><th className="py-2 pr-3 font-medium">Tests</th><th className="py-2 font-medium">Why</th>
            </tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id} className="border-t border-ink-800 align-top hover:bg-ink-850/60">
                  <td className="py-2.5 pr-3 max-w-[420px]"><div className="leading-snug">{r.statement}</div><div className="font-mono text-[10px] text-ink-400 mt-0.5">{r.source.split('/').pop()} · {r.pages.join(', ')}</div></td>
                  <td className="py-2.5 pr-3"><Badge color={r.needs === 'owner' ? '#a78bfa' : r.needs === 'authed' ? '#7c9cff' : '#7a87a6'}>{r.needs}</Badge></td>
                  <td className="py-2.5 pr-3"><Badge color={UI_COLOR[r.ui]}>{r.ui}</Badge></td>
                  <td className="py-2.5 pr-3">{r.tests ? <Badge color="#7c9cff">{r.tests} test{r.tests > 1 ? 's' : ''}</Badge> : <Badge color="#f87171">none</Badge>}</td>
                  <td className="py-2.5 text-xs text-ink-400 max-w-[420px] leading-relaxed">{r.partial ? <span className="text-orange-300">{r.partial}</span> : r.ui === 'weak' ? <span className="text-lime-300">only a shared CSS class matched (0.4, below the 0.45 review line)</span> : r.gap_reason || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && <Empty>Nothing matches.</Empty>}
        </div>
      </Card>
    </div>
  )
}
