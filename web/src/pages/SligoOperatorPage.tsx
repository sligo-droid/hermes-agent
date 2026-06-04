import { ArrowRight, CheckCircle2, Database, GitBranch, Sparkles, Wrench } from "lucide-react";
import { Link } from "react-router-dom";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";

const FLOW_STEPS = [
  "Cron prongs emit proposal contract",
  "Proposal cards are ingested into profile storage",
  "Operators approve or reject upstream proposals",
  "Approved cards link into Workers for execution",
  "Feedback summaries guide future prongs",
];

export default function SligoOperatorPage() {
  return (
    <div className="flex flex-col gap-6">
      <section className="relative overflow-hidden rounded-2xl border border-cyan-300/20 bg-[radial-gradient(circle_at_top_left,rgba(34,211,238,0.18),transparent_34%),linear-gradient(135deg,rgba(8,47,73,0.88),rgba(2,6,23,0.96))] p-5 shadow-2xl shadow-cyan-950/30 sm:p-7">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
          <div className="max-w-3xl">
            <Badge className="border-cyan-200/30 bg-cyan-200/10 px-2.5 py-1 text-[0.68rem] uppercase tracking-[0.18em] text-cyan-100">
              Sligo operator home
            </Badge>
            <h1 className="mt-4 text-3xl font-semibold tracking-tight text-white sm:text-4xl">
              Review proposals upstream. Execute through Hermes Workers.
            </h1>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-cyan-50/75">
              This v1 surface keeps Sligo's self-improvement loop inside Hermes: cron-generated proposal cards stay separate from the downstream Kanban worker board, while approvals create auditable worker tasks.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Link className="inline-flex items-center gap-2 rounded-md bg-cyan-100 px-4 py-2 text-sm font-medium text-slate-950 hover:bg-cyan-50" to="/self-improvement">
              Review Proposals <ArrowRight className="h-4 w-4" />
            </Link>
            <a className="inline-flex items-center rounded-md border border-cyan-100/25 px-4 py-2 text-sm font-medium text-cyan-50 hover:bg-cyan-100/10" href="/workers">
              Open Workers
            </a>
          </div>
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <Card className="border-current/15 bg-card/65">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg text-midground">
              <GitBranch className="h-5 w-5 text-cyan-200" /> Integrated Flow
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ol className="grid gap-3">
              {FLOW_STEPS.map((step, index) => (
                <li className="flex gap-3 rounded-lg border border-current/10 bg-black/20 p-3" key={step}>
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-cyan-200/35 bg-cyan-200/10 text-xs text-cyan-100">
                    {index + 1}
                  </span>
                  <span className="text-sm text-text-secondary">{step}</span>
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>

        <div className="grid gap-4">
          <Card className="border-cyan-200/20 bg-card/75">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base text-midground">
                <Sparkles className="h-4 w-4 text-cyan-200" /> Self-Improvement
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3 text-sm text-text-secondary">
              <p>Upstream board for PID proposal cards, parse failures, source runs, approve/reject actions, and worker links.</p>
              <Link className="inline-flex w-fit items-center rounded border border-current/20 px-3 py-1.5 text-sm text-text-secondary hover:text-midground" to="/self-improvement">
                Open board
              </Link>
            </CardContent>
          </Card>

          <Card className="border-current/15 bg-card/75">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base text-midground">
                <Wrench className="h-4 w-4 text-cyan-200" /> Workers
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3 text-sm text-text-secondary">
              <p>Downstream execution board remains the real Hermes worker surface; Sligo v1 links into it instead of replacing it.</p>
              <a className="inline-flex w-fit items-center rounded border border-current/20 px-3 py-1.5 text-sm text-text-secondary hover:text-midground" href="/workers">
                Open workers
              </a>
            </CardContent>
          </Card>
        </div>
      </div>

      <Card className="border-current/15 bg-card/55">
        <CardContent className="grid gap-3 py-4 text-sm text-text-secondary md:grid-cols-3">
          <div className="flex items-start gap-2">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-cyan-200" />
            <span>Authenticated approval is idempotent via <span className="font-mono-ui">self-improvement:&lt;proposal_id&gt;</span>.</span>
          </div>
          <div className="flex items-start gap-2">
            <Database className="mt-0.5 h-4 w-4 shrink-0 text-cyan-200" />
            <span>Proposal state lives under the active Hermes profile, not in a separate Sligo app.</span>
          </div>
          <div className="flex items-start gap-2">
            <Wrench className="mt-0.5 h-4 w-4 shrink-0 text-cyan-200" />
            <span>Public clean URLs are deployment configuration, outside this v1 repo change.</span>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
