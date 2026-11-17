import { useMemo, useState } from 'react'
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Network } from 'lucide-react'
import { useFetch } from '../api'
import { Badge, Card, Conf, Empty, KIND, Pill } from '../ui'

type GNode = { id: string; kind: string; label: string; confidence: number; observed: boolean; detail: any }
type GEdge = { src: string; dst: string; type: string; label?: string; confidence?: number }

const W = 200, H = 60

function RiskNode({ data }: { data: GNode & { selected: boolean } }) {
  const m = KIND[data.kind] ?? KIND.Endpoint
  return (
    <div className={`rounded-xl px-3 py-2 transition ${data.selected ? 'ring-2 ring-white/70' : ''}`}
      style={{ width: W, background: '#0f1422', border: `1px solid ${m.color}55`, boxShadow: `0 0 0 1px ${m.color}11, 0 8px 24px -12px ${m.color}66`, opacity: data.observed ? 1 : .7 }}>
      <Handle type="target" position={Position.Left} style={{ background: m.color, border: 0, width: 6, height: 6 }} />
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wider" style={{ color: m.color }}>{m.label}</span>
        <span className="font-mono text-[10px] text-ink-400">{data.confidence?.toFixed(2)}</span>
      </div>
      <div className="mt-0.5 text-[12px] leading-tight text-ink-100 line-clamp-2">{data.label}</div>
      <Handle type="source" position={Position.Right} style={{ background: m.color, border: 0, width: 6, height: 6 }} />
    </div>
  )
}
const nodeTypes = { risk: RiskNode }

export default function GraphView({ pr, setPr, reports }: { pr: number; setPr: (n: number) => void; reports: number[] }) {
  const { data, error, loading } = useFetch<{ nodes: GNode[]; edges: GEdge[] }>(`/api/graph/${pr}`, [pr])
  const [showTests, setShowTests] = useState(false)
  const [sel, setSel] = useState<string | null>(null)

  const { nodes, edges } = useMemo(() => {
    if (!data) return { nodes: [] as Node[], edges: [] as Edge[] }
    const keep = data.nodes.filter(n => showTests || n.kind !== 'Test')
    const ids = new Set(keep.map(n => n.id))
    const es = data.edges.filter(e => ids.has(e.src) && ids.has(e.dst) && e.type !== 'TESTED_BY')
    // Fixed columns, left to right: what changed -> what renders it -> what a user sees -> what it breaks.
    // Long columns (26 tests) wrap into side-by-side sub-columns so the graph stays readable at one zoom.
    const COLS = ['Method', 'TemplateElement', 'Element', 'Screen', 'Flow', 'Requirement', 'Test', 'Endpoint']
    const ROWS = 10
    let x = 0
    const nodes: Node[] = []
    for (const kind of COLS) {
      const col = keep.filter(n => n.kind === kind).sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0))
      if (!col.length) continue
      const sub = Math.ceil(col.length / ROWS)
      const rows = Math.min(ROWS, col.length)
      col.forEach((n, i) => {
        const c = Math.floor(i / ROWS), r = i % ROWS
        const yOff = ((ROWS - rows) * (H + 14)) / 2
        nodes.push({ id: n.id, type: 'risk', position: { x: x + c * (W + 20), y: yOff + r * (H + 14) }, data: { ...n, selected: n.id === sel } as any })
      })
      x += sub * (W + 20) + 56
    }
    const edges: Edge[] = es.map((e, i) => {
      const m = KIND[data.nodes.find(n => n.id === e.src)?.kind ?? ''] ?? KIND.Endpoint
      return {
        id: `${e.src}-${e.dst}-${i}`, source: e.src, target: e.dst, animated: e.type === 'RENDERS' || e.type === 'AFFECTS',
        style: { stroke: m.color, strokeOpacity: sel && (e.src === sel || e.dst === sel) ? 1 : .35, strokeWidth: sel && (e.src === sel || e.dst === sel) ? 2 : 1.2 },
        markerEnd: { type: MarkerType.ArrowClosed, color: m.color, width: 14, height: 14 },
      }
    })
    return { nodes, edges }
  }, [data, showTests, sel])

  const selected = data?.nodes.find(n => n.id === sel)
  const links = data?.edges.filter(e => e.src === sel || e.dst === sel) ?? []

  return (
    <div>
      <Card title={`Blast radius of PR #${pr}`} icon={<Network size={14} />}
        right={<div className="flex items-center gap-2">
          {reports.map(n => <Pill key={n} active={n === pr} onClick={() => { setPr(n); setSel(null) }}>#{n}</Pill>)}
          <span className="mx-1 h-4 w-px bg-ink-600" />
          <Pill active={showTests} onClick={() => setShowTests(!showTests)}>{showTests ? 'hide tests' : `show ${data?.nodes.filter(n => n.kind === 'Test').length ?? ''} tests`}</Pill>
        </div>}>
        <div className="mb-3 flex flex-wrap gap-2">
          {['Method', 'TemplateElement', 'Element', 'Screen', 'Flow', 'Test', 'Requirement'].map(k => <Badge key={k} color={KIND[k].color}>{KIND[k].label}</Badge>)}
          <span className="text-xs text-ink-400 ml-2">left to right: what changed → what renders it → what a user sees → what it breaks</span>
        </div>
        <div className="relative h-[700px] rounded-xl bg-ink-950/60 border border-ink-700">
          {loading && <div className="p-6 text-sm text-ink-400">Loading the subgraph from Neo4j…</div>}
          {error && <div className="p-6"><Empty>{error}</Empty></div>}
          {data && (
            <ReactFlowProvider>
              <ReactFlow key={`${pr}-${showTests}`} nodes={nodes} edges={edges} nodeTypes={nodeTypes} minZoom={0.15} maxZoom={1.4}
                fitView fitViewOptions={{ padding: 0.04, maxZoom: 1.15 }} proOptions={{ hideAttribution: true }}
                onNodeClick={(_, n) => setSel(n.id)} onPaneClick={() => setSel(null)}>
                <Background color="#1d2538" gap={22} />
                <Controls showInteractive={false} />
              </ReactFlow>
            </ReactFlowProvider>
          )}
          {!selected ? (
            <div className="pointer-events-none absolute right-4 top-4 rounded-xl glass px-4 py-2.5 text-xs text-ink-300">Click any node: why is it here?</div>
          ) : (
            <div className="absolute right-4 top-4 bottom-4 w-[340px] overflow-y-auto scroll-thin glass rounded-2xl p-5 shadow-2xl slide-in">
              <div className="flex items-center justify-between mb-3">
                <span className="text-[12px] font-semibold uppercase tracking-wider text-ink-300">Why is this here?</span>
                <button onClick={() => setSel(null)} className="text-ink-400 hover:text-ink-100 text-sm">✕</button>
              </div>
              <div className="space-y-4">
                <div>
                  <Badge color={KIND[selected.kind]?.color}>{KIND[selected.kind]?.label}</Badge>
                  <div className="mt-2 text-sm font-medium leading-snug">{selected.label}</div>
                  <div className="mt-1 font-mono text-[10px] text-ink-400 break-all">{selected.id}</div>
                </div>
                <Conf value={selected.confidence ?? 1} observed={selected.observed} />
                {!selected.observed && <Badge color="#f87171">known from code, never seen in the running app</Badge>}
                {selected.detail?.why && (
                  <div>
                    <div className="text-[11px] uppercase tracking-wider text-ink-400 mb-1">Evidence</div>
                    <ul className="space-y-1.5">{selected.detail.why.map((w: string, i: number) => <li key={i} className="rounded-lg bg-ink-850 px-3 py-2 text-xs leading-relaxed text-ink-300">{w}</li>)}</ul>
                  </div>
                )}
                {selected.detail?.file && <div className="text-xs text-ink-300 font-mono break-all">{selected.detail.file}{selected.detail.lines ? `:${selected.detail.lines}` : selected.detail.line ? `:${selected.detail.line}` : ''}</div>}
                {selected.detail?.lineage && <div className="text-xs text-warn">{selected.detail.lineage}</div>}
                <div>
                  <div className="text-[11px] uppercase tracking-wider text-ink-400 mb-1">Graph edges ({links.length})</div>
                  <div className="space-y-1">
                    {links.slice(0, 40).map((e, i) => {
                      const other = data!.nodes.find(n => n.id === (e.src === sel ? e.dst : e.src))
                      return <div key={i} className="text-[11px] text-ink-300"><span className="font-mono text-brand">{e.src === sel ? '→' : '←'} {e.type}</span> {other?.label}</div>
                    })}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </Card>

    </div>
  )
}
