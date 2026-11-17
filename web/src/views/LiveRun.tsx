import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Activity, Bot, Brain, CheckCircle2, CircleDashed, Coins, FileCode2, FileText, GitPullRequest, Loader2,
  Play, RefreshCw, Search, ShieldAlert, ShieldCheck, Sparkles, TriangleAlert, Wrench, Zap,
} from 'lucide-react'
import { getJSON, kfmt, useRun, usd, type Blast, type RunEvent } from '../api'
import { Badge, Card, Conf, Empty, Pill, kindMeta } from '../ui'

const STAGES = [
  { key: 'traverse', title: 'Map the diff & traverse the graph', sub: 'diff → methods → templates → UI → journeys, tests, requirements', icon: Search, llm: false },
  { key: 'assess', title: 'Assessment agent', sub: 'reads the diff and the graph with tools, re-ranks risk (grounded, bounded)', icon: Bot, llm: true },
  { key: 'report', title: 'Report writer + verifier', sub: 'LLM writes for a QA lead; code rejects invented ids and dropped items', icon: FileText, llm: true },
  { key: 'save', title: 'Write back to Neo4j', sub: 'PullRequest → CHANGES / PUTS_AT_RISK / LOSES_COVERAGE', icon: Sparkles, llm: false },
]
const TOOL_ICON: Record<string, any> = { read_diff: FileCode2, method_source: FileCode2, impacts: Activity, inspect: Search, finish: CheckCircle2 }

export default function LiveRun({ initialPr, onDone }: { initialPr: number; onDone: (pr: number) => void }) {
  const [pr, setPr] = useState(initialPr)
  const [custom, setCustom] = useState('')
  const [fresh, setFresh] = useState(true)
  const { events, running, error, start } = useRun()
  const [blast, setBlast] = useState<Blast | null>(null)
  const [md, setMd] = useState('')
  const [now, setNow] = useState(Date.now())
  const feedRef = useRef<HTMLDivElement>(null)

  useEffect(() => { if (!running) return; const t = setInterval(() => setNow(Date.now()), 100); return () => clearInterval(t) }, [running])
  useEffect(() => { feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: 'smooth' }) }, [events.length])

  const done = events.find(e => e.type === 'done')
  useEffect(() => {
    if (done?.ok && (events[0]?.run === 'pr')) {
      getJSON<{ blast: Blast; markdown: string }>(`/api/report/${events[0].pr}`).then(r => { setBlast(r.blast); setMd(r.markdown); onDone(events[0].pr) })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [done?.t])

  // load the last report for this PR so the page isn't empty before a run
  useEffect(() => {
    if (running || events.length) return
    getJSON<{ blast: Blast; markdown: string }>(`/api/report/${pr}`).then(r => { setBlast(r.blast); setMd(r.markdown) }).catch(() => { setBlast(null); setMd('') })
  }, [pr, running, events.length])

  const state = useMemo(() => {
    const st: Record<string, { status: 'idle' | 'running' | 'done' | 'fallback'; seconds: number; usd: number }> = {}
    STAGES.forEach(s => (st[s.key] = { status: 'idle', seconds: 0, usd: 0 }))
    let cost = 0, tin = 0, tout = 0, calls = 0
    let impacts: any = null, diff: any = null, assessment: any = null
    for (const e of events) {
      if (e.type === 'stage_start' && st[e.stage]) st[e.stage].status = 'running'
      if (e.type === 'stage_end' && st[e.stage]) { st[e.stage].seconds += e.seconds; if (st[e.stage].status !== 'fallback') st[e.stage].status = 'done' }
      if (e.type === 'fallback' && st[e.stage]) st[e.stage].status = 'fallback'
      if (e.type === 'llm_call') { cost += e.usd_uncached; tin += e.input; tout += e.output; calls++; if (st[e.stage]) st[e.stage].usd += e.usd_uncached }
      if (e.type === 'impacts') impacts = e
      if (e.type === 'diff') diff = e
      if (e.type === 'assessment') assessment = e
    }
    // a later stage started => earlier "running" ones are finished (write/verify/render share 'report')
    return { st, cost, tin, tout, calls, impacts, diff, assessment }
  }, [events])

  const t0 = events[0]?.t
  const elapsed = t0 ? ((done ? done.t * 1000 : now) - t0 * 1000) / 1000 : 0
  const runPr = (n: number) => { setPr(n); setBlast(null); setMd(''); start({ kind: 'pr', pr: n, fresh }) }

  return (
    <div className="space-y-5">
      {/* control bar */}
      <div className="glass rounded-2xl px-5 py-4 flex flex-wrap items-center gap-3">
        <GitPullRequest className="text-brand" size={18} />
        <span className="text-sm text-ink-300">Pull request</span>
        {[311, 268].map(n => <Pill key={n} active={pr === n} onClick={() => setPr(n)}>#{n}</Pill>)}
        <input
          value={custom} onChange={e => setCustom(e.target.value.replace(/\D/g, ''))} placeholder="other #"
          onKeyDown={e => { if (e.key === 'Enter' && custom) setPr(+custom) }}
          className="w-24 rounded-full bg-ink-800 px-3 py-1 text-xs outline-none focus:ring-1 ring-brand"
        />
        <label className="ml-2 flex items-center gap-2 text-xs text-ink-300 cursor-pointer select-none">
          <input type="checkbox" checked={fresh} onChange={e => setFresh(e.target.checked)} className="accent-[#7c9cff]" />
          live LLM calls (no cache)
        </label>
        <div className="flex-1" />
        <button
          disabled={running} onClick={() => runPr(custom ? +custom : pr)}
          className="flex items-center gap-2 rounded-xl bg-gradient-to-r from-brand to-brand-2 px-5 py-2 text-sm font-semibold text-ink-950 shadow-lg shadow-brand/20 disabled:opacity-50 hover:brightness-110 transition"
        >
          {running ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />} {running ? 'Running…' : `Analyse #${custom || pr}`}
        </button>
      </div>
      {error && <div className="rounded-xl border border-bad/40 bg-bad/10 px-4 py-3 text-sm text-bad">{error}</div>}

      <div className="grid grid-cols-12 gap-5">
        {/* pipeline */}
        <div className="col-span-12 xl:col-span-3 space-y-5">
          <Card title="Pipeline" icon={<Zap size={14} />} right={<span className="font-mono text-xs text-ink-400">{elapsed.toFixed(1)}s</span>}>
            <ol className="relative space-y-1">
              {STAGES.map((s, i) => {
                const x = state.st[s.key]
                const Icon = s.icon
                const color = x.status === 'done' ? '#34d399' : x.status === 'running' ? '#7c9cff' : x.status === 'fallback' ? '#f87171' : '#2a3550'
                return (
                  <li key={s.key} className="relative pl-12 pb-5">
                    {i < STAGES.length - 1 && <span className="absolute left-[19px] top-10 bottom-0 w-px" style={{ background: x.status === 'done' ? '#34d39966' : '#2a3550' }} />}
                    <span className={`absolute left-0 top-0 grid h-10 w-10 place-items-center rounded-xl border ${x.status === 'running' ? 'pulse-ring' : ''}`}
                      style={{ borderColor: color, background: `${color}1a`, color }}>
                      {x.status === 'running' ? <Loader2 size={17} className="animate-spin" /> : x.status === 'done' ? <CheckCircle2 size={17} /> : x.status === 'fallback' ? <TriangleAlert size={17} /> : <Icon size={17} />}
                    </span>
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{s.title}</span>
                      {s.llm && <Badge color="#a78bfa">LLM</Badge>}
                    </div>
                    <div className="text-[11px] text-ink-400 leading-snug mt-0.5">{s.sub}</div>
                    {(x.seconds > 0 || x.usd > 0) && (
                      <div className="mt-1 flex gap-3 font-mono text-[11px] text-ink-300">
                        <span>{x.seconds.toFixed(1)}s</span>{x.usd > 0 && <span>{usd(x.usd)}</span>}
                      </div>
                    )}
                  </li>
                )
              })}
            </ol>
          </Card>

          <Card title="The change" icon={<FileCode2 size={14} />}>
            {state.diff ? (
              <div className="space-y-3 slide-in">
                <div className="text-sm font-medium leading-snug">{state.diff.title}</div>
                {state.diff.notes?.map((n: any) => (
                  <div key={n.file} className={`rounded-lg px-3 py-2 text-xs ${n.scope === 'global' ? 'bg-warn/10 text-warn' : 'bg-ink-800 text-ink-300'}`}>
                    <span className="font-mono">{n.file}</span>: {n.message}
                  </div>
                ))}
                {state.diff.units?.map((u: any) => (
                  <div key={u.unit} className="rounded-lg bg-ink-800/70 px-3 py-2">
                    <div className="font-mono text-xs text-pink-300">{u.unit}</div>
                    <div className="font-mono text-[10px] text-ink-400 mt-0.5 truncate">{u.file} · lines {u.lines?.[0]}–{u.lines?.[u.lines.length - 1]}</div>
                    {u.lineage && <div className="text-[10px] text-warn mt-1">{u.lineage}</div>}
                  </div>
                ))}
              </div>
            ) : blast ? (
              <div className="space-y-2">
                <div className="text-sm font-medium">{blast.title}</div>
                {blast.changed.map(u => <div key={u.unit} className="font-mono text-xs text-pink-300">{u.unit}</div>)}
              </div>
            ) : <Empty>Pick a PR and press Analyse.</Empty>}
          </Card>
        </div>

        {/* live feed */}
        <div className="col-span-12 xl:col-span-5">
          <Card title="Live events" icon={<Activity size={14} />} className="h-full"
            right={<span className="flex items-center gap-2 text-xs text-ink-400">{running && <span className="h-2 w-2 rounded-full bg-ok animate-pulse" />}{events.length} events</span>}>
            <div ref={feedRef} className="scroll-thin h-[620px] overflow-y-auto pr-2 space-y-2">
              {events.length === 0 && <Empty>Events from the real pipeline stream here: stages, every LLM call with its tokens and cost, each agent tool call, and the verifier's verdict.</Empty>}
              {events.map((e, i) => <EventRow key={i} e={e} t0={t0} />)}
            </div>
          </Card>
        </div>

        {/* meters */}
        <div className="col-span-12 xl:col-span-4 space-y-5">
          <Card title="Cost & tokens" icon={<Coins size={14} />}>
            <div className="flex items-end justify-between">
              <div>
                <div className="text-4xl font-semibold tabular-nums tracking-tight">{usd(state.cost || blast?.cost?.llm.total.usd_uncached, 3)}</div>
                <div className="text-xs text-ink-400 mt-1">{events.length ? 'this run, from scratch' : 'last run of this PR'}</div>
              </div>
              <div className="text-right font-mono text-xs text-ink-300 space-y-1">
                <div>{kfmt(state.tin || blast?.cost?.llm.total.input_tokens || 0)} in</div>
                <div>{kfmt(state.tout || blast?.cost?.llm.total.output_tokens || 0)} out</div>
                <div>{state.calls || blast?.cost?.llm.total.calls || 0} model calls</div>
              </div>
            </div>
          </Card>

          <Card title="Blast radius" icon={<ShieldAlert size={14} />}>
            {(() => {
              const counts = state.impacts?.counts ?? (blast ? blast.impacts.reduce((a: any, i) => ((a[i.kind] = (a[i.kind] || 0) + 1), a), {}) : null)
              if (!counts) return <Empty>Counts appear the moment the traversal finishes.</Empty>
              const max = Math.max(...(Object.values(counts) as number[]))
              return (
                <div className="space-y-2.5 slide-in">
                  {['requirement', 'flow', 'test', 'element', 'screen', 'unmapped_code'].filter(k => counts[k]).map(k => {
                    const m = kindMeta(k)
                    return (
                      <div key={k} className="flex items-center gap-3">
                        <span className="w-28 text-xs text-ink-300">{m.label}</span>
                        <div className="h-2 flex-1 rounded-full bg-ink-800 overflow-hidden">
                          <div className="h-full rounded-full transition-all duration-700" style={{ width: `${(counts[k] / max) * 100}%`, background: m.color }} />
                        </div>
                        <span className="w-8 text-right font-mono text-xs">{counts[k]}</span>
                      </div>
                    )
                  })}
                </div>
              )
            })()}
          </Card>

          <Card title="What the change does" icon={<Brain size={14} />}>
            {state.assessment?.behaviour_change || blast?.assessment?.behaviour_change ? (
              <p className="text-sm leading-relaxed text-ink-100 slide-in">{state.assessment?.behaviour_change || blast?.assessment?.behaviour_change}</p>
            ) : <Empty>The assessment agent's reading of the diff.</Empty>}
          </Card>
        </div>
      </div>

      {blast && (
        <div className="grid grid-cols-12 gap-5 slide-in">
          <Card title="Re-test first" icon={<ShieldCheck size={14} />} className="col-span-12 xl:col-span-5">
            <div className="space-y-1.5 max-h-[520px] overflow-y-auto scroll-thin pr-2">
              {blast.impacts.filter(i => ['flow', 'test', 'requirement'].includes(i.kind)).slice(0, 30).map(i => {
                const m = kindMeta(i.kind)
                const moved = i.base_confidence != null && Math.abs(i.base_confidence - i.confidence) > 0.005
                return (
                  <div key={i.id} className="rounded-lg bg-ink-850 px-3 py-2 hover:bg-ink-800 transition">
                    <div className="flex items-center gap-2">
                      <Badge color={m.color}>{m.label}</Badge>
                      {!i.observed && <Badge color="#f87171">not seen in app</Badge>}
                      {moved && <Badge color="#a78bfa">re-ranked {i.base_confidence!.toFixed(2)}→{i.confidence.toFixed(2)}</Badge>}
                    </div>
                    <div className="mt-1 flex items-center gap-3">
                      <span className="flex-1 text-sm leading-snug">{i.label}</span>
                      <Conf value={i.confidence} observed={i.observed} />
                    </div>
                  </div>
                )
              })}
            </div>
          </Card>
          <Card title="Report for the QA lead" icon={<FileText size={14} />} className="col-span-12 xl:col-span-7"
            right={<a href={blast.url} target="_blank" className="text-xs text-brand hover:underline">PR on GitHub ↗</a>}>
            <div className="md max-h-[520px] overflow-y-auto scroll-thin pr-3"><ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown></div>
          </Card>
        </div>
      )}
    </div>
  )
}

function EventRow({ e, t0 }: { e: RunEvent; t0?: number }) {
  const ts = t0 ? `+${(e.t - t0).toFixed(1)}s` : ''
  const base = 'slide-in rounded-xl px-3 py-2 text-sm border'
  const T = <span className="font-mono text-[10px] text-ink-400 w-12 shrink-0">{ts}</span>
  switch (e.type) {
    case 'run_start':
      return <div className={`${base} border-brand/30 bg-brand/5 flex gap-3`}>{T}<Play size={14} className="text-brand mt-0.5" /><span>Run started: {e.run === 'pr' ? `PR #${e.pr}` : 'graph build'}{e.fresh ? ' · live LLM calls' : ' · cached LLM answers'}</span></div>
    case 'stage_start':
      return <div className="flex gap-3 px-3 py-1 text-xs text-ink-400">{T}<CircleDashed size={13} className="mt-0.5" /> {e.stage} · {e.node} started</div>
    case 'stage_end':
      return <div className="flex gap-3 px-3 py-1 text-xs text-ink-400">{T}<CheckCircle2 size={13} className="mt-0.5 text-ok" /> {e.stage} · {e.node} done in {e.seconds}s</div>
    case 'diff':
      return <div className={`${base} border-pink-400/20 bg-pink-400/5 flex gap-3`}>{T}<FileCode2 size={14} className="text-pink-300 mt-0.5" /><div><div>Diff mapped: {e.units?.length ?? 0} changed code unit(s) in {e.files?.length} file(s)</div>{e.notes?.length > 0 && <div className="text-xs text-warn mt-1">{e.notes.map((n: any) => n.message).join(' · ')}</div>}</div></div>
    case 'impacts':
      return <div className={`${base} border-cyan/20 bg-cyan/5 flex gap-3`}>{T}<Search size={14} className="text-cyan mt-0.5" /><div>Graph traversal: {Object.entries(e.counts).map(([k, v]) => `${v} ${k}`).join(' · ')}</div></div>
    case 'agent_turn':
      return <div className="flex gap-3 px-3 py-1 text-xs text-brand-2">{T}<Bot size={13} className="mt-0.5" /> agent turn {e.turn + 1}: {e.tools?.length ? `calls ${e.tools.join(', ')}` : 'thinking'}</div>
    case 'agent_step': {
      const Icon = TOOL_ICON[e.tool] ?? Wrench
      const input = e.input && Object.keys(e.input).length ? JSON.stringify(e.input) : ''
      return (
        <div className={`${base} border-brand-2/25 bg-brand-2/5`}>
          <div className="flex gap-3">{T}<Icon size={14} className="text-brand-2 mt-0.5" />
            <div className="min-w-0 flex-1">
              <div><span className="font-mono text-brand-2">{e.tool}</span>{e.tool === 'finish' ? ' · submits the assessment' : ''}</div>
              {input && e.tool !== 'finish' && <div className="font-mono text-[10.5px] text-ink-400 truncate">{input.slice(0, 220)}</div>}
              {e.tool === 'finish' && e.input?.behaviour_change && <div className="text-xs text-ink-300 mt-1">{e.input.behaviour_change}</div>}
              {e.output && <div className="mt-1 font-mono text-[10.5px] text-ink-300/80 line-clamp-2 whitespace-pre-wrap">{e.output.slice(0, 260)}</div>}
            </div>
          </div>
        </div>
      )
    }
    case 'llm_call':
      return (
        <div className="flex gap-3 px-3 py-1 text-xs">{T}<Brain size={13} className="mt-0.5 text-brand-2" />
          <span className="text-ink-300">{e.stage} · {e.kind === 'agent_turn' ? 'agent turn' : 'structured call'}{e.cached ? ' (cached)' : ''}</span>
          <span className="ml-auto font-mono text-ink-400">{kfmt(e.input)} in · {kfmt(e.output)} out · {usd(e.usd_uncached)} · {e.seconds}s</span>
        </div>
      )
    case 'grounding_retry':
      return <div className={`${base} border-warn/30 bg-warn/10 flex gap-3 text-warn`}>{T}<ShieldAlert size={14} className="mt-0.5" /><div>Grounding check: {e.ungrounded.length} adjustment(s) on items the agent never inspected. Sent back: "inspect these, then resubmit".</div></div>
    case 'assessment':
      return (
        <div className={`${base} border-brand-2/30 bg-brand-2/10`}>
          <div className="flex gap-3">{T}<Bot size={14} className="text-brand-2 mt-0.5" />
            <div>Assessment: {e.applied?.length ?? 0} re-ranked, {e.rejected?.length ?? 0} rejected by the guard{e.skipped ? ` · skipped: ${e.skipped}` : ''}</div>
          </div>
        </div>
      )
    case 'verify':
      return e.ok
        ? <div className={`${base} border-ok/30 bg-ok/10 flex gap-3 text-ok`}>{T}<ShieldCheck size={14} className="mt-0.5" /> Verifier: every cited id exists, every must-mention item is in the report</div>
        : <div className={`${base} border-warn/30 bg-warn/10 flex gap-3 text-warn`}>{T}<ShieldAlert size={14} className="mt-0.5" /><div>Verifier rejected the draft: {e.errors?.slice(0, 3).join('; ')}. Rewriting.</div></div>
    case 'fallback':
      return <div className={`${base} border-bad/40 bg-bad/10 flex gap-3 text-bad`}>{T}<TriangleAlert size={14} className="mt-0.5" /> Fallback in {e.stage}: {e.error}</div>
    case 'report':
      return <div className={`${base} border-ok/30 bg-ok/10 flex gap-3`}>{T}<FileText size={14} className="text-ok mt-0.5" /> Report ready ({e.writer} writer) · saved to reports/pr-{e.pr}.md and Neo4j</div>
    case 'done':
      return e.ok
        ? <div className={`${base} border-ok/40 bg-ok/15 flex gap-3 font-medium`}>{T}<CheckCircle2 size={14} className="text-ok mt-0.5" /> Done in {e.seconds}s</div>
        : <div className={`${base} border-bad/40 bg-bad/10 flex gap-3 text-bad`}>{T}<TriangleAlert size={14} className="mt-0.5" /> {e.error}</div>
    default:
      return null
  }
}

export function RebuildButton() {
  const { running, start } = useRun()
  return (
    <button disabled={running} onClick={() => start({ kind: 'build' })} className="flex items-center gap-1.5 rounded-lg bg-ink-800 px-3 py-1.5 text-xs text-ink-300 hover:bg-ink-700 disabled:opacity-50">
      <RefreshCw size={13} className={running ? 'animate-spin' : ''} /> {running ? 'Rebuilding…' : 'Rebuild graph'}
    </button>
  )
}
