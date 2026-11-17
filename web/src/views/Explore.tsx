import { useState } from 'react'
import { Ban, Bot, CheckCircle2, Eye, Globe, MousePointerClick, Shield, Target, Trash2, Type } from 'lucide-react'
import { useFetch, usd } from '../api'
import { Badge, Card, Empty, Stat } from '../ui'

const ICON: Record<string, any> = { observe: Eye, click: MousePointerClick, fill: Type, goto: Globe, finish: CheckCircle2 }

export default function Explore() {
  const { data, error } = useFetch<any>('/api/explore')
  const [open, setOpen] = useState<number | null>(null)
  if (error) return <Empty>{error}</Empty>
  if (!data) return <div className="text-sm text-ink-400">Loading…</div>
  const cost = data.cost?.llm?.total
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <Stat label="Owner-only screens reached" value={data.recorded.length} accent="#a78bfa" sub="pages only an article's author sees" />
        <Stat label="Tool calls" value={data.trace.length} sub="observe · click · fill · goto · finish" />
        <Stat label="Writes blocked" value={data.blocked_writes.length} accent="#f87171" sub="aborted at the network layer" />
        <Stat label="Test article" value={data.deleted.every((d: any) => d.verified_gone) ? 'deleted ✓' : 'check!'} accent="#34d399" sub="re-fetched: 404 = really gone" />
        <Stat label="Agent cost" value={cost ? usd(cost.usd_uncached) : '—'} sub={data.cost ? `${data.cost.seconds_total}s, mostly browser` : ''} />
      </div>

      <div className="grid grid-cols-12 gap-5">
        <Card className="col-span-12 xl:col-span-7" title="The agent's run, step by step" icon={<Bot size={14} />}>
          <div className="mb-4 rounded-xl bg-ink-850 p-4">
            <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-ink-400"><Target size={13} /> Goals it was given (open gaps)</div>
            <ul className="mt-2 space-y-1.5">{data.goals.map((g: string, i: number) => <li key={i} className="text-xs text-ink-300 leading-relaxed">• {g}</li>)}</ul>
          </div>
          <ol className="relative space-y-2">
            {data.trace.map((s: any, i: number) => {
              const Icon = ICON[s.tool] ?? Bot
              const blocked = typeof s.output === 'string' && s.output.includes('BLOCKED')
              return (
                <li key={i} className="relative pl-11">
                  <span className={`absolute left-0 top-1 grid h-8 w-8 place-items-center rounded-lg border ${blocked ? 'border-bad/60 bg-bad/10 text-bad' : 'border-brand-2/40 bg-brand-2/10 text-brand-2'}`}><Icon size={15} /></span>
                  <button onClick={() => setOpen(open === i ? null : i)} className="w-full text-left rounded-xl bg-ink-850 hover:bg-ink-800 px-3 py-2 transition">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm text-brand-2">{s.tool}</span>
                      {s.input && Object.keys(s.input).length > 0 && s.tool !== 'finish' && <span className="font-mono text-xs text-ink-300 truncate">{JSON.stringify(s.input)}</span>}
                      {blocked && <Badge color="#f87171">write blocked</Badge>}
                      <span className="ml-auto text-[10px] text-ink-400">turn {s.turn + 1}</span>
                    </div>
                    {s.tool === 'finish' && <div className="mt-1 text-xs text-ink-300 leading-relaxed whitespace-pre-wrap">{s.input?.summary}</div>}
                    {open === i && s.output && <pre className="mt-2 max-h-64 overflow-auto scroll-thin whitespace-pre-wrap font-mono text-[10.5px] text-ink-400">{s.output}</pre>}
                  </button>
                </li>
              )
            })}
          </ol>
        </Card>

        <div className="col-span-12 xl:col-span-5 space-y-5">
          <Card title="Guardrails (enforced in code)" icon={<Shield size={14} />}>
            <ul className="space-y-3 text-sm">
              <li className="flex gap-3"><Ban size={16} className="text-bad mt-0.5 shrink-0" /><div><div>Network-level write policy</div><div className="text-xs text-ink-400 mt-0.5">Only two calls pass: create ONE article titled "br-test: …", and delete what this run created. Everything else is aborted and the agent is told it was blocked.</div>
                <div className="mt-1.5 flex flex-wrap gap-1.5">{data.blocked_writes.map((w: string) => <Badge key={w} color="#f87171">{w}</Badge>)}</div></div></li>
              <li className="flex gap-3"><Trash2 size={16} className="text-ok mt-0.5 shrink-0" /><div><div>Verified cleanup</div><div className="text-xs text-ink-400 mt-0.5">In a finally block, through the API; re-fetched to confirm 404; retried once; leftovers from a crashed run are swept at the start of the next.</div>
                <div className="mt-1.5 flex flex-wrap gap-1.5">{data.deleted.map((d: any) => <Badge key={d.slug} color={d.verified_gone ? '#34d399' : '#f87171'}>{d.slug} · {d.delete_status} · {d.verified_gone ? 'gone' : 'NOT gone'}</Badge>)}</div></div></li>
              <li className="flex gap-3"><Eye size={16} className="text-brand mt-0.5 shrink-0" /><div><div>Owner pages recorded with all writes off</div><div className="text-xs text-ink-400 mt-0.5">Each new owner-only page is crawled in a separate tab, buttons dry-run clicked: that's how we saw the owner's Publish send PUT.</div></div></li>
            </ul>
          </Card>
          <Card title="What it reached" icon={<Eye size={14} />}>
            <div className="grid grid-cols-1 gap-3">
              {data.recorded.map((sid: string) => (
                <figure key={sid} className="rounded-xl overflow-hidden border border-ink-700 bg-ink-950">
                  {data.screenshots[sid] && <img src={`/${data.screenshots[sid]}`} className="w-full max-h-56 object-cover object-top" />}
                  <figcaption className="px-3 py-2 text-xs font-mono text-ink-300">{sid}</figcaption>
                </figure>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
