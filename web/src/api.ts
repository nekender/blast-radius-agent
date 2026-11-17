import { useCallback, useEffect, useRef, useState } from 'react'

export type Impact = {
  kind: 'element' | 'screen' | 'flow' | 'requirement' | 'test' | 'unmapped_code'
  id: string
  label: string
  confidence: number
  why: string[]
  observed: boolean
  base_confidence?: number | null
}

export type Blast = {
  pr: number
  title: string
  url: string
  changed: { file: string; unit: string; lines: number[]; resolved: string[]; lineage?: string | null }[]
  impacts: Impact[]
  coverage_lost: { requirement: string; statement: string; why: string; kind?: string }[]
  needs_review: { item: string; why: string; confidence: number }[]
  notes: { file: string; scope: string; message: string }[]
  assessment?: {
    behaviour_change?: string
    applied?: { id: string; from: number; to: number; reason: string; capped?: boolean }[]
    rejected?: { id: string; rejected_because: string }[]
    tool_calls?: number
    skipped?: string
  } | null
  cost?: {
    llm: { model: string; total: CostTotals; by_stage: Record<string, CostTotals> }
    seconds_by_stage: Record<string, number>
    seconds_total: number
  } | null
}

export type CostTotals = {
  calls: number; api_calls: number; input_tokens: number; output_tokens: number
  usd: number; usd_uncached: number; llm_seconds: number
}

export type RunEvent = { type: string; t: number; [k: string]: any }

export async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${url}`)
  return r.json()
}

export function useFetch<T>(url: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const reload = useCallback(() => {
    if (!url) return
    setLoading(true)
    getJSON<T>(url)
      .then(d => { setData(d); setError(null) })
      .catch(e => { setError(String(e.message || e)); setData(null) })
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, ...deps])
  useEffect(() => { reload() }, [reload])
  return { data, error, loading, reload }
}

/** Start a real pipeline run on the server and stream its events (Server-Sent Events). */
export function useRun() {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const es = useRef<EventSource | null>(null)

  const start = useCallback(async (body: { kind?: 'pr' | 'build'; pr?: number; fresh?: boolean }) => {
    setEvents([]); setError(null); setRunning(true)
    es.current?.close()
    const r = await fetch('/api/run', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
    if (!r.ok) {
      setError((await r.json().catch(() => ({}))).detail || 'could not start the run')
      setRunning(false)
      return
    }
    const source = new EventSource('/api/stream')
    es.current = source
    source.onmessage = (m) => {
      const e = JSON.parse(m.data) as RunEvent
      setEvents(prev => [...prev, e])
      if (e.type === 'done') {
        setRunning(false)
        if (!e.ok) setError(e.error)
        source.close()
      }
    }
    source.onerror = () => { source.close(); setRunning(false) }
  }, [])

  useEffect(() => () => es.current?.close(), [])
  return { events, running, error, start }
}

export const usd = (n: number | undefined | null, digits = 3) => `$${(n ?? 0).toFixed(digits)}`
export const pct = (n: number) => `${Math.round(n * 100)}%`
export const kfmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`)
