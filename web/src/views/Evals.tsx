import { FlaskConical, Gauge } from 'lucide-react'
import { pct, useFetch, usd } from '../api'
import { Badge, Card, Empty } from '../ui'

export default function Evals() {
  const { data, error } = useFetch<{ results: Record<string, any>; gold: Record<string, any> }>('/api/evals')
  if (error) return <Empty>{error}</Empty>
  if (!data) return <div className="text-sm text-ink-400">Loading…</div>
  return (
    <div className="space-y-5">
      <Card title="How a run is judged" icon={<FlaskConical size={14} />}>
        <ol className="grid grid-cols-1 lg:grid-cols-5 gap-3 text-sm">
          {[
            ['Deterministic stages', 'The traversal is hashed every run. Any difference is a bug, not noise.'],
            ['Gold sets', 'Hand-labelled must / acceptable / must-not items: recall, precision, hard failures.'],
            ['Agent checks', 'Top journey and top test must be right; ungrounded adjustments counted.'],
            ['Report checks', 'No invented ids; every must-mention item present.'],
            ['pass^k', 'Every run correct. A QA lead sees one report, so 4 of 5 is a failing tool.'],
          ].map(([h, b], i) => (
            <li key={h} className="rounded-xl bg-ink-850 p-4"><div className="text-[11px] text-brand font-mono">LAYER {i + 1}</div><div className="mt-1 font-medium">{h}</div><div className="mt-1 text-xs text-ink-400 leading-relaxed">{b}</div></li>
          ))}
        </ol>
      </Card>
      <div className="grid grid-cols-12 gap-5">
        {Object.entries(data.results).map(([pr, r]) => {
          const s = r.summary, runs = r.runs as any[]
          const maxUsd = Math.max(...runs.map(x => x.usd ?? 0), 0.01)
          return (
            <Card key={pr} className="col-span-12 xl:col-span-6" title={`PR #${pr}`} icon={<Gauge size={14} />}
              right={<Badge color={s['pass^k'] ? '#34d399' : '#f87171'} solid>{s['pass^k'] ? 'pass^k ✓' : 'pass^k ✗'}</Badge>}>
              <div className="grid grid-cols-4 gap-3">
                {[
                  ['runs', s.runs], ['recall', pct(s.recall)], ['precision', pct(s.precision)],
                  ['deterministic', s.deterministic_stage_identical ? 'identical' : 'DIFFERS'],
                  ['report checks', s.report_pass_rate == null ? '—' : pct(s.report_pass_rate)],
                  ['top journey', s.agent_top_flow_ok_rate == null ? 'n/a' : pct(s.agent_top_flow_ok_rate)],
                  ['top test', s.agent_top_test_ok_rate == null ? 'n/a' : pct(s.agent_top_test_ok_rate)],
                  ['ungrounded', s.agent_ungrounded_adjustments ?? 0],
                ].map(([k, v]) => <div key={k as string} className="rounded-xl bg-ink-850 px-3 py-2"><div className="text-[10px] uppercase tracking-wider text-ink-400">{k}</div><div className="text-lg font-semibold tabular-nums">{v as any}</div></div>)}
              </div>
              <div className="mt-5">
                <div className="flex items-baseline justify-between text-xs text-ink-400"><span>Cost per run (fresh agent + report)</span><span>mean {usd(s.usd_per_run_mean)} · median {s.seconds_per_run_median}s</span></div>
                <div className="mt-3 flex items-end gap-3 h-36">
                  {runs.map((x, i) => (
                    <div key={i} className="flex-1 h-full flex flex-col items-center justify-end gap-1.5">
                      <span className="font-mono text-[10px] text-ink-300">{usd(x.usd)}</span>
                      <div className="w-full rounded-t-lg bg-gradient-to-t from-brand/30 to-brand-2 transition-all" style={{ height: `${(x.usd / maxUsd) * 78}%`, minHeight: 4 }} />
                      <span className="text-[10px] text-ink-400">run {i + 1} · {x.seconds}s</span>
                    </div>
                  ))}
                </div>
              </div>
              {(s.missed?.length > 0 || s.unexpected?.length > 0) && (
                <div className="mt-4 text-xs text-ink-400">
                  {s.missed?.length > 0 && <div>Missed: {s.missed.map((m: any) => JSON.stringify(m)).join(', ')}</div>}
                  {s.unexpected?.length > 0 && <div>Not in the gold set: {s.unexpected.join(', ')}</div>}
                </div>
              )}
              {data.gold[pr]?.note && <div className="mt-4 rounded-lg bg-ink-850 px-3 py-2 text-xs text-ink-300 leading-relaxed">{data.gold[pr].note}</div>}
            </Card>
          )
        })}
      </div>
    </div>
  )
}
