import { Background, Handle, MarkerType, Position, ReactFlow, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Boxes, Camera, Layers } from 'lucide-react'
import { useFetch, usd } from '../api'
import { Badge, Card, Stat } from '../ui'

type Box = { title: string; sub: string; who: 'code' | 'llm' | 'agent' | 'store'; }
const WHO = { code: ['#7a87a6', 'code'], llm: ['#a78bfa', 'LLM, checked'], agent: ['#f472b6', 'agent'], store: ['#22d3ee', 'graph'] } as const

function StageNode({ data }: { data: Box }) {
  const [c, label] = WHO[data.who]
  return (
    <div className="rounded-xl px-4 py-3 w-[210px]" style={{ background: '#0f1422', border: `1px solid ${c}66`, boxShadow: `0 10px 30px -15px ${c}` }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <Handle type="target" position={Position.Top} id="t" style={{ opacity: 0 }} />
      <div className="flex items-center justify-between"><span className="text-[13px] font-semibold">{data.title}</span><Badge color={c}>{label}</Badge></div>
      <div className="mt-1 text-[11px] leading-snug text-ink-400">{data.sub}</div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Bottom} id="b" style={{ opacity: 0 }} />
    </div>
  )
}

const N = (id: string, x: number, y: number, d: Box): Node => ({ id, position: { x, y }, data: d as any, type: 'stage', draggable: false })
const nodes: Node[] = [
  N('crawl', 0, 0, { title: '1 · Crawl', sub: 'Playwright, logged out + in; dry-run clicks; accessibility fingerprints', who: 'code' }),
  N('explore', 0, 150, { title: '1b · Exploration agent', sub: 'reaches owner-only pages; network write policy; verified cleanup', who: 'agent' }),
  N('spec', 0, 300, { title: '2 · Spec', sub: 'tables parsed by code, prose → requirements by LLM', who: 'llm' }),
  N('tests', 0, 450, { title: '2b · Test suite', sub: '129 Playwright tests → steps → controls', who: 'code' }),
  N('code', 0, 600, { title: '3 · Code map', sub: 'routes, methods, templates, bindings, endpoints', who: 'code' }),
  N('link', 300, 300, { title: '4 · Link layers', sub: 'scored code↔UI, evidence pieces, absence as Gap nodes', who: 'code' }),
  N('neo', 600, 300, { title: 'Neo4j graph', sub: 'requirements · UI · tests · code · PRs', who: 'store' }),
  N('blast', 900, 150, { title: '5 · Blast radius', sub: 'diff → methods → traversal → confidence', who: 'code' }),
  N('assess', 900, 300, { title: '5b · Assessment agent', sub: 'reads diff + graph via tools; grounded, bounded re-rank', who: 'agent' }),
  N('report', 900, 450, { title: '6 · Report + verifier', sub: 'LLM writes; code rejects invented ids', who: 'llm' }),
]
const E = (s: string, t: string, animated = true, sh?: string, th?: string): Edge => ({
  id: `${s}-${t}`, source: s, target: t, animated, sourceHandle: sh, targetHandle: th,
  style: { stroke: '#7c9cff', strokeOpacity: .55 }, markerEnd: { type: MarkerType.ArrowClosed, color: '#7c9cff' },
})
const edges: Edge[] = [
  E('crawl', 'link'), E('explore', 'link'), E('spec', 'link'), E('tests', 'link'), E('code', 'link'),
  E('link', 'neo'), E('neo', 'blast'), E('blast', 'assess', true, 'b', 't'), E('assess', 'report', true, 'b', 't'),
]

export default function System() {
  const { data } = useFetch<any>('/api/overview')
  const { data: screens } = useFetch<any[]>('/api/screens')
  const s = data?.summary ?? {}
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 lg:grid-cols-6 gap-4">
        <Stat label="Screens" value={s.screens ?? '—'} sub="route template × login state" />
        <Stat label="Controls" value={s.elements ?? '—'} sub={`${s.elements_linked_to_code ?? '—'} linked to code`} />
        <Stat label="Requirements" value={s.requirements ?? '—'} sub={`${s.covered ?? '—'} shown (+${s.covered_weak_class_only ?? 0} weak) · ${s.requirements_tested ?? '—'} tested`} accent="#34d399" />
        <Stat label="Tests" value={s.tests ?? '—'} sub={`${s.test_steps ?? '—'} steps parsed`} />
        <Stat label="Journeys" value={s.flows_observed ?? '—'} sub="observed, from sign-in" />
        <Stat label="Build cost" value={data?.build_cost ? usd(data.build_cost.llm.total.usd_uncached) : '—'} sub="from scratch; $0 cached" />
      </div>
      <Card title="Architecture" icon={<Layers size={14} />} right={<div className="flex gap-2">{Object.entries(WHO).map(([k, [c, l]]) => <Badge key={k} color={c}>{l}</Badge>)}</div>}>
        <div className="h-[720px] rounded-xl bg-ink-950/60 border border-ink-700">
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={{ stage: StageNode }} fitView proOptions={{ hideAttribution: true }}
            nodesDraggable={false} panOnDrag zoomOnScroll={false}>
            <Background color="#1d2538" gap={22} />
          </ReactFlow>
        </div>
        <p className="mt-3 text-sm text-ink-400">The rule for every stage: deterministic where the answer is a fact in the code or the app; an LLM where the task needs judgment or reads prose; every LLM output checked by code before it counts.</p>
      </Card>
      <Card title="What the crawler saw" icon={<Camera size={14} />} right={<span className="text-xs text-ink-400"><Boxes size={12} className="inline mr-1" />{screens?.length ?? 0} screens</span>}>
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-4">
          {screens?.map(sc => (
            <figure key={sc.id} className="rounded-xl overflow-hidden border border-ink-700 bg-ink-950 hover:border-brand/60 transition">
              {sc.screenshot && <img src={`/${sc.screenshot}`} loading="lazy" className="h-36 w-full object-cover object-top" />}
              <figcaption className="px-3 py-2 flex items-center justify-between">
                <span className="font-mono text-[11px] truncate">{sc.template}</span>
                <span className="flex gap-1"><Badge color={sc.auth === 'owner' ? '#a78bfa' : sc.auth === 'authed' ? '#7c9cff' : '#7a87a6'}>{sc.auth}</Badge>{sc.states > 1 && <Badge color="#fbbf24">{sc.states} states</Badge>}</span>
              </figcaption>
            </figure>
          ))}
        </div>
      </Card>
    </div>
  )
}
