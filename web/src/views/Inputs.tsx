import type { ReactNode } from 'react'
import { ArrowRight, BookOpen, Code2, ExternalLink, FlaskConical, GitPullRequest, Globe, Package } from 'lucide-react'
import { useFetch } from '../api'
import { Badge, Card } from '../ui'

function Ext({ href, children }: { href?: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer"
      className="inline-flex items-center gap-1.5 rounded-lg bg-brand/15 px-3 py-1.5 text-xs font-medium text-brand hover:bg-brand/25 transition">
      {children} <ExternalLink size={12} />
    </a>
  )
}

function Num({ v, l }: { v: ReactNode; l: string }) {
  return <div className="rounded-xl bg-ink-850 px-3 py-2"><div className="text-lg font-semibold tabular-nums">{v}</div><div className="text-[10.5px] uppercase tracking-wider text-ink-400">{l}</div></div>
}

function Step({ n, title, icon, sub, children }: { n: number; title: string; icon: ReactNode; sub: string; children: ReactNode }) {
  return (
    <Card title={<span className="flex items-center gap-2"><span className="grid h-6 w-6 place-items-center rounded-full bg-gradient-to-br from-brand to-brand-2 text-[11px] font-bold text-ink-950">{n}</span>{title}</span>}
      icon={icon} className="h-full">
      <p className="-mt-1 mb-4 text-sm text-ink-300 leading-relaxed">{sub}</p>
      {children}
    </Card>
  )
}

export default function Inputs({ goTo }: { goTo: (tab: string) => void }) {
  const { data } = useFetch<any>('/api/inputs')
  if (!data) return <div className="text-sm text-ink-400">Loading…</div>
  const { app, code, spec, tests, prs, project } = data
  return (
    <div className="space-y-5">
      <div className="glass rounded-2xl px-6 py-5 flex flex-wrap items-center gap-4">
        <div className="flex-1 min-w-[320px]">
          <div className="text-lg font-semibold">What goes in</div>
          <p className="text-sm text-ink-300 mt-1 leading-relaxed">
            A real, public app with all four layers tied together: the running site, the repo that deploys it, a spec that lists what must be on
            every page, and its own Playwright test suite. Then a real pull request from its history.
          </p>
        </div>
        <Ext href={project.url}>This project on GitHub</Ext>
        <Ext href={project.design}>Design doc</Ext>
      </div>

      <div className="grid grid-cols-12 gap-5">
        <div className="col-span-12 xl:col-span-6">
          <Step n={1} title="The app under test" icon={<Globe size={14} />} sub="Conduit, the RealWorld demo: a Medium-style blog (articles, comments, profiles, follows, favourites). Angular frontend, public API.">
            <div className="grid grid-cols-[1fr_auto] gap-4 items-start">
              {app.screenshot && <a href={app.url} target="_blank" rel="noreferrer"><img src={`/${app.screenshot}`} className="rounded-xl border border-ink-700 w-full h-56 object-cover object-top hover:border-brand/60 transition" /></a>}
              <div className="space-y-2 w-36">
                <Num v={app.screens} l="screens crawled" />
                <Num v={app.elements} l="controls found" />
              </div>
            </div>
            <div className="mt-4 flex flex-wrap gap-2"><Ext href={app.url}>{app.url.replace('https://', '')}</Ext><Ext href={`${app.url}/article/introduction-to-machine-learning-for-developers`}>an article page</Ext></div>
          </Step>
        </div>

        <div className="col-span-12 xl:col-span-6">
          <Step n={2} title="Its source code" icon={<Code2 size={14} />} sub="The repo that deploys that exact site. Parsed at any commit: routes, components, methods with line ranges, template bindings, API calls.">
            <div className="grid grid-cols-3 gap-2">
              <Num v={code.routes} l="routes" /><Num v={code.components} l="components" /><Num v={code.methods} l="methods" />
              <Num v={code.template_elements} l="template elements" /><Num v={code.services} l="services" /><Num v={code.endpoints} l="API endpoints" />
            </div>
            <div className="mt-3 flex flex-wrap gap-1.5">{code.route_list.map((r: string, i: number) => <span key={i} className="font-mono text-[11px] rounded-md bg-ink-850 px-2 py-0.5 text-ink-300">{r}</span>)}</div>
            <div className="mt-4 flex flex-wrap items-center gap-2"><Ext href={code.url}>{code.repo}</Ext><span className="font-mono text-[11px] text-ink-400">HEAD {code.head}</span></div>
          </Step>
        </div>

        <div className="col-span-12 xl:col-span-6">
          <Step n={3} title="Its product spec" icon={<BookOpen size={14} />} sub="What the app must contain, page by page. Because it lists features, the system can say something is missing, not just that it found things.">
            <div className="space-y-2">
              {spec.files.map((f: any) => (
                <a key={f.name} href={f.url} target="_blank" rel="noreferrer" className="flex items-center gap-3 rounded-xl bg-ink-850 px-4 py-3 hover:bg-ink-800 transition">
                  <span className="font-mono text-sm">{f.name}</span>
                  <Badge color={f.read_by.includes('code') ? '#7a87a6' : '#a78bfa'}>{f.read_by}</Badge>
                  <span className="ml-auto text-sm tabular-nums text-ink-300">{f.requirements ? `${f.requirements} requirements` : ''}</span>
                  <ExternalLink size={13} className="text-ink-400" />
                </a>
              ))}
            </div>
            <div className="mt-4 flex items-center gap-3"><Ext href={spec.docs}>docs.realworld.show</Ext><span className="text-sm text-ink-300">{spec.requirements} requirements in the graph</span></div>
          </Step>
        </div>

        <div className="col-span-12 xl:col-span-6">
          <Step n={4} title="Its test suite" icon={<FlaskConical size={14} />} sub="RealWorld's own Playwright end-to-end tests. Each test is read as steps (on page X, act on selector Y) and linked to the controls it touches.">
            <div className="grid grid-cols-3 gap-2"><Num v={tests.tests} l="tests" /><Num v={tests.steps} l="steps parsed" /><Num v={tests.linked} l="linked to controls" /></div>
            <div className="mt-3 flex flex-wrap gap-1.5">{tests.files.map((f: string) => <span key={f} className="font-mono text-[11px] rounded-md bg-ink-850 px-2 py-0.5 text-ink-300">{f}</span>)}</div>
            <div className="mt-4"><Ext href={tests.url}>realworld/specs/e2e</Ext></div>
          </Step>
        </div>

        <div className="col-span-12">
          <Step n={5} title="The pull requests we analyse" icon={<GitPullRequest size={14} />} sub="Real merged PRs from the app's history. #311 is the main demo; #268 was held out while building and changes files that have since been renamed.">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {prs.map((p: any) => (
                <div key={p.number} className="rounded-xl bg-ink-850 p-4">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-brand">#{p.number}</span>
                    <span className="text-sm font-medium flex-1">{p.title.replace(/^#\s*/, '')}</span>
                    <Ext href={p.url}>diff</Ext>
                  </div>
                  <div className="mt-2 space-y-1">{p.files.map((f: string) => <div key={f} className="font-mono text-[11px] text-ink-400">{f}</div>)}</div>
                  <div className="mt-2 text-[11px] text-ink-400">merge commit {p.merge_commit}</div>
                </div>
              ))}
            </div>
          </Step>
        </div>
      </div>

      <div className="glass rounded-2xl px-6 py-4 flex items-center gap-4">
        <Package size={18} className="text-brand" />
        <span className="text-sm text-ink-300 flex-1">All four are joined into one Neo4j graph. Next: how the system is built, then a live run on #311.</span>
        <button onClick={() => goTo('system')} className="flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-brand to-brand-2 px-4 py-2 text-sm font-semibold text-ink-950">System <ArrowRight size={14} /></button>
      </div>
    </div>
  )
}
