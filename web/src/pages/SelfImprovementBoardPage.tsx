import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ExternalLink,
  FileText,
  GitBranch,
  Inbox,
  Layers3,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type {
  SelfImprovementProposalCard,
  SelfImprovementProposalRun,
  SelfImprovementProposalsResponse,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type DetailMode = "card" | "source" | "failure";
type ActionKind = "approve" | "reject";

type Selection =
  | { mode: "card"; card: SelfImprovementProposalCard }
  | { mode: "source" | "failure"; run: SelfImprovementProposalRun };

interface ActiveAction {
  proposalId: string;
  kind: ActionKind;
}

function formatTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function truncate(value: string, max = 220): string {
  return value.length > max ? `${value.slice(0, max).trimEnd()}...` : value;
}

function cardMeta(card: SelfImprovementProposalCard): string[] {
  return [
    card.project ? `project ${card.project}` : null,
    card.prong ? `prong ${card.prong}` : null,
    card.run_id ? `run ${card.run_id}` : null,
    card.cron_job_id ? `cron ${card.cron_job_id}` : null,
  ].filter((value): value is string => Boolean(value));
}

function priorityTone(priority: string): string {
  const normalized = priority.toLowerCase();
  if (["critical", "urgent", "p0", "p1"].includes(normalized)) return "border-red-300/50 bg-red-400/10 text-red-100";
  if (normalized === "high") return "border-orange-300/50 bg-orange-400/10 text-orange-100";
  if (normalized === "medium") return "border-amber-300/50 bg-amber-300/10 text-amber-100";
  return "border-slate-300/20 bg-white/[0.04] text-slate-300";
}

function statusTone(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "approved" || normalized === "enqueued") return "border-emerald-300/50 bg-emerald-400/10 text-emerald-100";
  if (normalized === "rejected") return "border-red-300/50 bg-red-400/10 text-red-100";
  return "border-cyan-200/35 bg-cyan-300/10 text-cyan-100";
}

function isApproved(card: SelfImprovementProposalCard): boolean {
  const normalized = card.status.toLowerCase();
  return normalized === "approved" || normalized === "enqueued" || Boolean(card.kanban_task_id);
}

function isRejected(card: SelfImprovementProposalCard): boolean {
  return card.status.toLowerCase() === "rejected" || Boolean(card.archived_at);
}

function isActionable(card: SelfImprovementProposalCard): boolean {
  return !isApproved(card) && !isRejected(card);
}

function StatusPill({ value }: { value: string }) {
  return (
    <Badge className={cn("w-fit border px-2 py-0.5 text-[0.68rem] uppercase tracking-[0.16em]", statusTone(value))}>
      {value || "unknown"}
    </Badge>
  );
}

function MetricCard({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.055] px-4 py-3 shadow-inner shadow-white/[0.02]">
      <div className={cn("text-2xl font-semibold tracking-tight text-white", tone)}>{value}</div>
      <div className="mt-1 text-[0.68rem] uppercase tracking-[0.18em] text-slate-400">{label}</div>
    </div>
  );
}

function EmptyState({ error, onRetry }: { error: string | null; onRetry: () => void }) {
  return (
    <Card className="border-white/10 bg-white/[0.035] shadow-2xl shadow-black/30">
      <CardContent className="flex flex-col items-center gap-3 py-20 text-center">
        {error ? <AlertTriangle className="h-9 w-9 text-amber-200" /> : <Inbox className="h-9 w-9 text-cyan-100/80" />}
        <div className="text-xl font-semibold text-white">
          {error ? "Proposal board failed to load" : "No proposal tickets yet"}
        </div>
        <p className="max-w-xl text-sm leading-6 text-slate-400">
          {error
            ? error
            : "Structured cron proposals will land here as ticket cards with their own approve and reject controls. Downstream worker execution stays separate."}
        </p>
        {error && (
          <Button size="sm" onClick={onRetry}>
            Retry
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

function ActionButton({
  kind,
  disabled,
  busy,
  onClick,
}: {
  kind: ActionKind;
  disabled: boolean;
  busy: boolean;
  onClick: () => void;
}) {
  const approve = kind === "approve";
  return (
    <button
      className={cn(
        "inline-flex h-9 items-center justify-center gap-2 rounded-full border px-3 text-xs font-semibold uppercase tracking-[0.14em] transition",
        approve
          ? "border-emerald-300/40 bg-emerald-300/12 text-emerald-50 hover:border-emerald-200/70 hover:bg-emerald-300/20"
          : "border-red-300/35 bg-red-300/10 text-red-50 hover:border-red-200/70 hover:bg-red-300/18",
        disabled && "cursor-not-allowed opacity-45 hover:bg-transparent",
      )}
      disabled={disabled}
      onClick={onClick}
      type="button"
    >
      {busy ? <Spinner className="text-sm" /> : approve ? <Check className="h-3.5 w-3.5" /> : <X className="h-3.5 w-3.5" />}
      {approve ? "Approve" : "Reject"}
    </button>
  );
}

function ProposalTicket({
  activeAction,
  card,
  onApprove,
  onLoadRun,
  onReject,
  onSelect,
  selected,
}: {
  activeAction: ActiveAction | null;
  card: SelfImprovementProposalCard;
  onApprove: (card: SelfImprovementProposalCard) => void;
  onLoadRun: (runId: number, mode: DetailMode) => void;
  onReject: (card: SelfImprovementProposalCard) => void;
  onSelect: () => void;
  selected: boolean;
}) {
  const meta = cardMeta(card);
  const approved = isApproved(card);
  const actionable = isActionable(card);
  const approveBusy = activeAction?.proposalId === card.proposal_id && activeAction.kind === "approve";
  const rejectBusy = activeAction?.proposalId === card.proposal_id && activeAction.kind === "reject";
  const anyBusy = activeAction !== null;

  return (
    <article
      className={cn(
        "group rounded-3xl border bg-slate-950/60 p-4 shadow-2xl shadow-black/20 transition",
        "hover:-translate-y-0.5 hover:border-cyan-100/35 hover:bg-slate-900/70",
        selected ? "border-cyan-100/60 ring-1 ring-cyan-100/20" : "border-white/10",
      )}
    >
      <button className="block w-full text-left" onClick={onSelect} type="button">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap gap-1.5">
              <StatusPill value={card.status} />
              <Badge className={cn("w-fit border px-2 py-0.5 text-[0.68rem] uppercase tracking-[0.16em]", priorityTone(card.priority))}>
                {card.priority}
              </Badge>
              {card.severity && (
                <Badge className="w-fit border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[0.68rem] uppercase tracking-[0.16em] text-slate-300">
                  {card.severity}
                </Badge>
              )}
            </div>
            <h3 className="text-base font-semibold leading-snug text-white group-hover:text-cyan-50">
              {card.title}
            </h3>
          </div>
          {approved && <CheckCircle2 className="mt-1 h-5 w-5 shrink-0 text-emerald-200" />}
        </div>
        <p className="mt-3 text-sm leading-6 text-slate-300">
          {truncate(card.summary || card.body || "No summary provided.")}
        </p>
        <div className="mt-4 flex flex-wrap gap-2 text-[0.68rem] uppercase tracking-[0.15em] text-slate-500">
          <span>{formatTime(card.created_at)}</span>
          {meta.slice(0, 3).map((item) => (
            <span key={item} className="max-w-full truncate">
              {item}
            </span>
          ))}
        </div>
      </button>

      <div className="mt-4 flex flex-col gap-3 border-t border-white/10 pt-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 text-xs text-slate-500">
          {card.kanban_task_id ? (
            <span className="font-mono-ui text-emerald-100">worker {card.kanban_task_id}</span>
          ) : (
            <span className="font-mono-ui">{card.proposal_id}</span>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className="inline-flex h-9 items-center justify-center rounded-full border border-white/10 px-3 text-xs font-semibold uppercase tracking-[0.14em] text-slate-300 transition hover:border-cyan-100/40 hover:text-cyan-50"
            onClick={() => onLoadRun(card.run_db_id, "source")}
            type="button"
          >
            Source
          </button>
          {card.worker_url && (
            <a
              className="inline-flex h-9 items-center justify-center gap-1 rounded-full border border-white/10 px-3 text-xs font-semibold uppercase tracking-[0.14em] text-slate-300 transition hover:border-emerald-200/50 hover:text-emerald-50"
              href={card.worker_url}
            >
              Worker <ExternalLink className="h-3 w-3" />
            </a>
          )}
          <ActionButton
            busy={approveBusy}
            disabled={!actionable || anyBusy}
            kind="approve"
            onClick={() => onApprove(card)}
          />
          <ActionButton
            busy={rejectBusy}
            disabled={!actionable || anyBusy}
            kind="reject"
            onClick={() => onReject(card)}
          />
        </div>
      </div>
    </article>
  );
}

function DetailPanel({
  activeAction,
  actionMessage,
  onApprove,
  onReject,
  onLoadRun,
  selection,
}: {
  activeAction: ActiveAction | null;
  actionMessage: string | null;
  onApprove: (card: SelfImprovementProposalCard) => void;
  onReject: (card: SelfImprovementProposalCard) => void;
  onLoadRun: (runId: number, mode: DetailMode) => void;
  selection: Selection | null;
}) {
  if (!selection) {
    return (
      <Card className="border-white/10 bg-slate-950/55 shadow-2xl shadow-black/20 xl:sticky xl:top-24">
        <CardContent className="flex min-h-96 flex-col items-center justify-center gap-3 text-center text-sm text-slate-400">
          <FileText className="h-9 w-9 text-slate-600" />
          <div className="max-w-xs">Pick a ticket for full evidence. Decisions no longer require this panel; every ticket carries approve and reject.</div>
        </CardContent>
      </Card>
    );
  }

  if (selection.mode === "card") {
    const { card } = selection;
    const actionable = isActionable(card);
    const approveBusy = activeAction?.proposalId === card.proposal_id && activeAction.kind === "approve";
    const rejectBusy = activeAction?.proposalId === card.proposal_id && activeAction.kind === "reject";
    return (
      <Card className="border-white/10 bg-slate-950/70 shadow-2xl shadow-black/25 xl:sticky xl:top-24">
        <CardHeader>
          <div className="flex flex-wrap gap-2">
            <StatusPill value={card.status} />
            {card.severity && <StatusPill value={card.severity} />}
          </div>
          <CardTitle className="text-xl leading-tight text-white">{card.title}</CardTitle>
        </CardHeader>
        <CardContent className="flex max-h-[calc(100dvh-9rem)] flex-col gap-5 overflow-y-auto text-sm">
          <section>
            <h4 className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">Summary</h4>
            <p className="leading-6 text-slate-300">{card.summary || "No summary provided."}</p>
          </section>
          {card.rationale && (
            <section>
              <h4 className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">Rationale</h4>
              <p className="leading-6 text-slate-300">{card.rationale}</p>
            </section>
          )}
          {card.source_excerpts.length > 0 && (
            <section>
              <h4 className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">Evidence</h4>
              <div className="space-y-2">
                {card.source_excerpts.map((excerpt, idx) => (
                  <blockquote key={`${idx}:${excerpt.text.slice(0, 24)}`} className="rounded-2xl border border-white/10 bg-white/[0.035] p-3 text-slate-300">
                    {excerpt.label && <div className="mb-1 text-xs uppercase tracking-[0.14em] text-slate-500">{excerpt.label}</div>}
                    {excerpt.text}
                  </blockquote>
                ))}
              </div>
            </section>
          )}
          <section>
            <h4 className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">Ticket metadata</h4>
            <dl className="grid grid-cols-[7rem_1fr] gap-2 text-xs text-slate-400">
              <dt>Project</dt><dd className="truncate text-white">{card.project}</dd>
              <dt>Prong</dt><dd className="truncate text-white">{card.prong}</dd>
              <dt>Proposal</dt><dd className="truncate font-mono-ui">{card.proposal_id}</dd>
              <dt>Run</dt><dd className="truncate font-mono-ui">{card.run_id || card.run_db_id}</dd>
              <dt>Cron job</dt><dd className="truncate font-mono-ui">{card.cron_job_id || "-"}</dd>
              <dt>Idempotency</dt><dd className="truncate font-mono-ui">{card.idempotency_key || "-"}</dd>
              <dt>Worker</dt><dd className="truncate font-mono-ui">{card.kanban_task_id || "-"}</dd>
            </dl>
          </section>
          {card.worker_url && (
            <a className="inline-flex w-fit items-center gap-1 text-sm text-emerald-100 underline underline-offset-4" href={card.worker_url}>
              Open worker task <ExternalLink className="h-3 w-3" />
            </a>
          )}
          {actionMessage && <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-3 text-xs text-slate-300">{actionMessage}</div>}
          <div className="flex flex-wrap gap-2 border-t border-white/10 pt-4">
            <Button size="sm" onClick={() => onLoadRun(card.run_db_id, "source")}>View source run</Button>
            <ActionButton
              busy={approveBusy}
              disabled={!actionable || activeAction !== null}
              kind="approve"
              onClick={() => onApprove(card)}
            />
            <ActionButton
              busy={rejectBusy}
              disabled={!actionable || activeAction !== null}
              kind="reject"
              onClick={() => onReject(card)}
            />
          </div>
        </CardContent>
      </Card>
    );
  }

  const { run } = selection;
  const source = selection.mode === "failure" ? run.parse_error : run.source_markdown;
  return (
    <Card className="border-white/10 bg-slate-950/70 shadow-2xl shadow-black/25 xl:sticky xl:top-24">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          {selection.mode === "failure" && <AlertTriangle className="h-4 w-4 text-amber-200" />}
          <StatusPill value={run.status} />
        </div>
        <CardTitle className="text-xl text-white">
          {selection.mode === "failure" ? "Parse failure" : "Source cron output"}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex max-h-[calc(100dvh-9rem)] flex-col gap-4 overflow-y-auto text-sm">
        <dl className="grid grid-cols-[6rem_1fr] gap-2 text-xs text-slate-400">
          <dt>Project</dt><dd className="truncate text-white">{run.project || "-"}</dd>
          <dt>Prong</dt><dd className="truncate text-white">{run.prong || "-"}</dd>
          <dt>Run</dt><dd className="truncate font-mono-ui">{run.run_id || run.id}</dd>
          <dt>Cron job</dt><dd className="truncate font-mono-ui">{run.cron_job_id || "-"}</dd>
          <dt>Cards</dt><dd>{run.card_count}</dd>
          <dt>Updated</dt><dd>{formatTime(run.updated_at)}</dd>
        </dl>
        {run.source_url && (
          <a className="inline-flex items-center gap-1 text-sm text-cyan-100 underline underline-offset-4" href={run.source_url} rel="noreferrer" target="_blank">
            Open source <ExternalLink className="h-3 w-3" />
          </a>
        )}
        <pre className="max-h-[55dvh] overflow-auto whitespace-pre-wrap rounded-2xl border border-white/10 bg-black/40 p-3 font-mono-ui text-xs leading-relaxed text-slate-300">
          {source || "No source output recorded."}
        </pre>
      </CardContent>
    </Card>
  );
}

export default function SelfImprovementBoardPage() {
  const [data, setData] = useState<SelfImprovementProposalsResponse | null>(null);
  const [failures, setFailures] = useState<SelfImprovementProposalRun[]>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [activeAction, setActiveAction] = useState<ActiveAction | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadBoard = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [proposals, parseFailures] = await Promise.all([
        api.getSelfImprovementProposals(),
        api.getSelfImprovementParseFailures(),
      ]);
      setData(proposals);
      setFailures(parseFailures.failures);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadBoard();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadBoard]);

  const cards = useMemo(
    () => data?.projects.flatMap((project) => project.prongs.flatMap((prong) => prong.cards)) ?? [],
    [data],
  );

  const metrics = useMemo(() => {
    const actionable = cards.filter(isActionable).length;
    const approved = cards.filter(isApproved).length;
    const highPriority = cards.filter((card) => ["critical", "urgent", "high"].includes(card.priority.toLowerCase())).length;
    const prongCount = data?.projects.reduce((count, project) => count + project.prongs.length, 0) ?? 0;
    return { actionable, approved, highPriority, prongCount };
  }, [cards, data]);

  const loadRun = useCallback(async (runId: number, mode: DetailMode) => {
    setDetailLoading(true);
    try {
      const response = await api.getSelfImprovementRun(runId);
      setSelection(mode === "failure" ? { mode: "failure", run: response.run } : { mode: "source", run: response.run });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const replaceCard = useCallback((next: SelfImprovementProposalCard | null) => {
    if (!next) return;
    setSelection({ mode: "card", card: next });
    setData((current) => {
      if (!current) return current;
      return {
        projects: current.projects.map((project) => ({
          ...project,
          prongs: project.prongs.map((prong) => ({
            ...prong,
            cards: prong.cards.map((card) => card.proposal_id === next.proposal_id ? next : card),
          })),
        })),
      };
    });
  }, []);

  const approveCard = useCallback(async (card: SelfImprovementProposalCard) => {
    setActiveAction({ proposalId: card.proposal_id, kind: "approve" });
    setActionMessage(null);
    try {
      const response = await api.approveSelfImprovementProposal(card.proposal_id);
      replaceCard(response.card);
      setActionMessage(`Approved into Kanban task ${response.card.kanban_task_id || "unknown"}.`);
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setActiveAction(null);
    }
  }, [replaceCard]);

  const rejectCard = useCallback(async (card: SelfImprovementProposalCard) => {
    const reason = window.prompt("Reason for rejecting this proposal?")?.trim();
    if (!reason) return;
    setActiveAction({ proposalId: card.proposal_id, kind: "reject" });
    setActionMessage(null);
    try {
      const response = await api.rejectSelfImprovementProposal(card.proposal_id, reason);
      setSelection((current) => current?.mode === "card" && current.card.proposal_id === response.card.proposal_id ? null : current);
      setData((current) => {
        if (!current) return current;
        return {
          projects: current.projects.map((project) => ({
            ...project,
            prongs: project.prongs
              .map((prong) => ({
                ...prong,
                cards: prong.cards.filter((item) => item.proposal_id !== response.card.proposal_id),
              }))
              .filter((prong) => prong.cards.length > 0),
          })).filter((project) => project.prongs.length > 0),
        };
      });
      setActionMessage("Proposal rejected and archived from the default board view.");
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setActiveAction(null);
    }
  }, []);

  return (
    <div className="mx-auto flex w-full max-w-[1500px] flex-col gap-6">
      <section className="relative overflow-hidden rounded-[2rem] border border-white/10 bg-[radial-gradient(circle_at_top_left,rgba(34,211,238,0.2),transparent_34%),radial-gradient(circle_at_bottom_right,rgba(16,185,129,0.16),transparent_30%),linear-gradient(135deg,rgba(2,6,23,0.96),rgba(8,13,28,0.94))] p-5 shadow-2xl shadow-black/40 sm:p-7">
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-cyan-200/60 to-transparent" />
        <div className="flex flex-col gap-6 xl:flex-row xl:items-end xl:justify-between">
          <div className="max-w-4xl">
            <Badge className="border-cyan-200/35 bg-cyan-200/10 px-3 py-1 text-[0.68rem] uppercase tracking-[0.2em] text-cyan-100">
              Self-improvement command deck
            </Badge>
            <h1 className="mt-5 max-w-3xl text-4xl font-semibold tracking-tight text-white sm:text-5xl">
              Decide on proposals where the tickets live.
            </h1>
            <p className="mt-4 max-w-3xl text-sm leading-6 text-slate-300 sm:text-base">
              Cron-generated recommendations land as operator tickets. Approve creates real downstream work; reject archives the proposal with feedback. The evidence drawer is for inspection, not a required control path.
            </p>
          </div>
          <div className="grid min-w-0 grid-cols-2 gap-3 sm:grid-cols-4 xl:min-w-[32rem]">
            <MetricCard label="Tickets" value={cards.length} />
            <MetricCard label="Actionable" tone="text-cyan-100" value={metrics.actionable} />
            <MetricCard label="High priority" tone="text-orange-100" value={metrics.highPriority} />
            <MetricCard label="Parse failures" tone={failures.length ? "text-amber-100" : "text-slate-100"} value={failures.length} />
          </div>
        </div>
      </section>

      <div className="flex flex-col gap-3 rounded-3xl border border-white/10 bg-white/[0.03] p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-[0.16em] text-slate-400">
          <span className="inline-flex items-center gap-2 rounded-full border border-white/10 px-3 py-1.5">
            <ShieldCheck className="h-3.5 w-3.5 text-emerald-200" /> idempotent approvals
          </span>
          <span className="inline-flex items-center gap-2 rounded-full border border-white/10 px-3 py-1.5">
            <Layers3 className="h-3.5 w-3.5 text-cyan-200" /> {metrics.prongCount} prongs
          </span>
          <span className="inline-flex items-center gap-2 rounded-full border border-white/10 px-3 py-1.5">
            <GitBranch className="h-3.5 w-3.5 text-slate-300" /> workers stay downstream
          </span>
        </div>
        <Button className="w-fit" disabled={loading} ghost size="sm" onClick={() => void loadBoard()}>
          {loading ? <Spinner /> : <RefreshCw className="h-4 w-4" />} Refresh
        </Button>
      </div>

      {actionMessage && (
        <div className="rounded-2xl border border-white/10 bg-white/[0.045] px-4 py-3 text-sm text-slate-300">
          {actionMessage}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-24"><Spinner className="text-2xl text-primary" /></div>
      ) : !data || error || (cards.length === 0 && failures.length === 0) ? (
        <EmptyState error={error} onRetry={() => void loadBoard()} />
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(23rem,30rem)]">
          <div className="flex min-w-0 flex-col gap-5">
            {failures.length > 0 && (
              <Card className="border-amber-300/35 bg-amber-950/20 shadow-2xl shadow-black/20">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-base text-amber-50">
                    <AlertTriangle className="h-4 w-4 text-amber-200" /> Parse failures need cleanup
                  </CardTitle>
                </CardHeader>
                <CardContent className="grid gap-2">
                  {failures.map((failure) => (
                    <button
                      className="rounded-2xl border border-amber-200/15 bg-black/20 p-3 text-left text-sm text-amber-50/80 hover:border-amber-200/50"
                      key={failure.id}
                      onClick={() => void loadRun(failure.id, "failure")}
                      type="button"
                    >
                      <div className="font-medium text-white">{failure.cron_job_name || failure.cron_job_id || failure.source_key}</div>
                      <div className="mt-1 line-clamp-2">{failure.parse_error || "Malformed proposal output"}</div>
                    </button>
                  ))}
                </CardContent>
              </Card>
            )}

            {data.projects.map((project) => (
              <section className="rounded-[1.75rem] border border-white/10 bg-white/[0.025] p-4" key={project.project}>
                <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
                  <div>
                    <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Project</p>
                    <h2 className="mt-1 text-2xl font-semibold tracking-tight text-white">{project.project}</h2>
                  </div>
                  <span className="rounded-full border border-white/10 px-3 py-1 text-xs uppercase tracking-[0.16em] text-slate-400">
                    {project.prongs.reduce((count, prong) => count + prong.cards.length, 0)} tickets
                  </span>
                </div>
                <div className="grid gap-4 2xl:grid-cols-2">
                  {project.prongs.map((prong) => (
                    <div className="rounded-[1.5rem] border border-white/10 bg-black/20 p-3" key={`${project.project}:${prong.prong}`}>
                      <div className="mb-3 flex items-center justify-between gap-3 px-1">
                        <h3 className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
                          {prong.prong}
                        </h3>
                        <span className="text-xs text-slate-500">{prong.cards.length}</span>
                      </div>
                      <div className="grid gap-3">
                        {prong.cards.map((card) => (
                          <ProposalTicket
                            activeAction={activeAction}
                            card={card}
                            key={card.proposal_id}
                            onApprove={approveCard}
                            onLoadRun={loadRun}
                            onReject={rejectCard}
                            onSelect={() => setSelection({ mode: "card", card })}
                            selected={selection?.mode === "card" && selection.card.proposal_id === card.proposal_id}
                          />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>

          <div className="min-w-0">
            {detailLoading ? (
              <Card className="border-white/10 bg-slate-950/55"><CardContent className="flex min-h-96 items-center justify-center"><Spinner /></CardContent></Card>
            ) : (
              <DetailPanel
                activeAction={activeAction}
                actionMessage={actionMessage}
                onApprove={approveCard}
                onLoadRun={loadRun}
                onReject={rejectCard}
                selection={selection}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
