import type { ReactNode } from 'react'

export const KIND: Record<string, { label: string; color: string; bg: string }> = {
  Method: { label: 'Changed code', color: '#f472b6', bg: 'rgba(244,114,182,.12)' },
  TemplateElement: { label: 'Template', color: '#c084fc', bg: 'rgba(192,132,252,.12)' },
  Element: { label: 'Live control', color: '#7c9cff', bg: 'rgba(124,156,255,.12)' },
  Screen: { label: 'Screen', color: '#22d3ee', bg: 'rgba(34,211,238,.10)' },
  Flow: { label: 'User journey', color: '#34d399', bg: 'rgba(52,211,153,.10)' },
  Test: { label: 'Test', color: '#fbbf24', bg: 'rgba(251,191,36,.10)' },
  Requirement: { label: 'Requirement', color: '#fb923c', bg: 'rgba(251,146,60,.10)' },
  Endpoint: { label: 'API call', color: '#94a3b8', bg: 'rgba(148,163,184,.10)' },
}
const KIND_OF_IMPACT: Record<string, string> = {
  element: 'Element', screen: 'Screen', flow: 'Flow', requirement: 'Requirement', test: 'Test', unmapped_code: 'Endpoint',
}
export const kindMeta = (k: string) => KIND[KIND_OF_IMPACT[k] ?? k] ?? KIND.Endpoint

export function Card({ title, icon, right, children, className = '' }: {
  title?: ReactNode; icon?: ReactNode; right?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <section className={`glass rounded-2xl ${className}`}>
      {title && (
        <header className="flex items-center justify-between px-5 pt-4 pb-3">
          <div className="flex items-center gap-2 text-[13px] font-semibold tracking-wide text-ink-300 uppercase">
            {icon}{title}
          </div>
          {right}
        </header>
      )}
      <div className={title ? 'px-5 pb-5' : 'p-5'}>{children}</div>
    </section>
  )
}

export function Stat({ label, value, sub, accent }: { label: string; value: ReactNode; sub?: ReactNode; accent?: string }) {
  return (
    <div className="glass rounded-2xl px-5 py-4">
      <div className="text-[11px] uppercase tracking-wider text-ink-400">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums" style={{ color: accent }}>{value}</div>
      {sub && <div className="mt-1 text-xs text-ink-400">{sub}</div>}
    </div>
  )
}

export function Badge({ children, color = '#7a87a6', solid = false }: { children: ReactNode; color?: string; solid?: boolean }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-[2px] text-[11px] font-medium whitespace-nowrap"
      style={{ color: solid ? '#0b0f19' : color, background: solid ? color : `${color}1f`, border: `1px solid ${color}40` }}
    >
      {children}
    </span>
  )
}

export function Conf({ value, observed = true }: { value: number; observed?: boolean }) {
  const c = value >= 0.75 ? '#34d399' : value >= 0.45 ? '#fbbf24' : '#f87171'
  return (
    <div className="flex items-center gap-2 min-w-[110px]">
      <div className="h-1.5 flex-1 rounded-full bg-ink-700 overflow-hidden">
        <div className="h-full rounded-full transition-all duration-700" style={{ width: `${Math.round(value * 100)}%`, background: c, opacity: observed ? 1 : .55 }} />
      </div>
      <span className="text-[11px] tabular-nums text-ink-300 w-8 text-right">{value.toFixed(2)}</span>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-xl border border-dashed border-ink-600 p-8 text-center text-sm text-ink-400">{children}</div>
}

export function Pill({ active, onClick, children }: { active?: boolean; onClick?: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-3 py-1 text-xs font-medium transition ${active ? 'bg-brand text-ink-950' : 'bg-ink-800 text-ink-300 hover:bg-ink-700'}`}
    >
      {children}
    </button>
  )
}
