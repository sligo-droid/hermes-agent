import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  CircleDot,
  ExternalLink,
  FileText,
  GitBranch,
  Inbox,
  ListChecks,
  PauseCircle,
  RefreshCw,
  RotateCcw,
  Send,
  Sparkles,
  Workflow,
  Wrench,
  X,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type {
  CommandCenterRun,
  CommandCenterSnapshot,
  CommandCenterSource,
  CommandCenterWorkItem,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type ViewKey = "overview" | "inbox" | "work" | "runs" | "recommendations" | "sources";
type Selection =
  | { kind: "work"; item: CommandCenterWorkItem }
  | { kind: "source"; source: CommandCenterSource }
  | { kind: "run"; run: CommandCenterRun };

type ActionKind = "approve" | "reject" | "halt" | "undo";

const TABS: Array<{ key: ViewKey; label: string; path: string; icon: typeof Inbox }> = [
  { key: "overview", label: "Overview", path: "/sligo", icon: Workflow },
  { key: "inbox", label: "Inbox", path: "/sligo/inbox", icon: Inbox },
  { key: "work", label: "Work", path: "/sligo/work", icon: ListChecks },
  { key: "runs", label: "Runs", path: "/sligo/runs", icon: Wrench },
  { key: "recommendations", label: "Recommendations", path: "/sligo/recommendations", icon: Sparkles },
  { key: "sources", label: "Sources", path: "/sligo/sources", icon: FileText },
];

function viewFromPath(pathname: string): ViewKey {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  if (normalized === "/self-improvement") return "recommendations";
  if (normalized.includes("/inbox")) return "inbox";
  if (normalized.includes("/work")) return "work";
  if (normalized.includes("/runs")) return "runs";
  if (normalized.includes("/recommendations")) return "recommendations";
  if (normalized.includes("/sources")) return "sources";
  return "overview";
}

function formatTime(value?: string | number | null): string {
  if (value === null || value === undefined || value === "") return "-";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function statusTone(status?: string | null): string {
  const normalized = String(status || "unknown").toLowerCase();
  if (["proposed", "parse_failed"].includes(normalized)) return "border-cyan-200/45 bg-cyan-300/10 text-cyan-100";
  if (["queued", "accepted"].includes(normalized)) return "border-blue-200/35 bg-blue-400/10 text-blue-100";
  if (["running", "review"].includes(normalized)) return "border-amber-200/45 bg-amber-300/10 text-amber-100";
  if (["blocked", "missing", "error"].includes(normalized)) return "border-red-300/45 bg-red-400/10 text-red-100";
  if (["shipped", "done"].includes(normalized)) return "border-emerald-300/45 bg-emerald-400/10 text-emerald-100";
  if (["rejected", "archived"].includes(normalized)) return "border-slate-400/30 bg-slate-400/10 text-slate-300";
  return "border-slate-400/25 bg-slate-400/10 text-slate-200";
}

function StatusPill({ value }: { value?: string | null }) {
  return (
    <Badge className={cn("w-fit border px-2 py-0.5 text-[0.66rem] uppercase tracking-[0.16em]", statusTone(value))}>
      {value || "unknown"}
    </Badge>
  );
}

function metric(snapshot: CommandCenterSnapshot | null, key: keyof CommandCenterSnapshot["metrics"]): number {
  const value = snapshot?.metrics?.[key];
  return typeof value === "number" ? value : 0;
}

function isInboxItem(item: CommandCenterWorkItem): boolean {
  return item.status === "proposed" || item.status === "blocked";
}

function isWorkItem(item: CommandCenterWorkItem): boolean {
  return item.status !== "proposed" && item.status !== "rejected" && item.status !== "archived";
}

function initialSelectionForView(snapshot: CommandCenterSnapshot, activeView: ViewKey): Selection | null {
  if (activeView === "runs") {
    const run = snapshot.runs[0];
    return run ? { kind: "run", run } : null;
  }
  if (activeView === "sources") {
    const source = snapshot.sources[0];
    return source ? { kind: "source", source } : null;
  }
  if (activeView === "recommendations") {
    const item = snapshot.work_items.find((candidate) => candidate.source.kind === "self_improvement");
    return item ? { kind: "work", item } : null;
  }
  if (activeView === "inbox") {
    const item = snapshot.work_items.find(isInboxItem);
    if (item) return { kind: "work", item };
    const source = snapshot.sources.find((candidate) => candidate.bucket === "inbox");
    return source ? { kind: "source", source } : null;
  }
  if (activeView === "work") {
    const item = snapshot.work_items.find(isWorkItem);
    return item ? { kind: "work", item } : null;
  }
  const item = snapshot.work_items[0];
  if (item) return { kind: "work", item };
  const source = snapshot.sources[0];
  if (source) return { kind: "source", source };
  const run = snapshot.runs[0];
  return run ? { kind: "run", run } : null;
}

function MetricCard({ label, value, detail, tone }: { label: string; value: number | string; detail?: string; tone?: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.055] px-4 py-3 shadow-inner shadow-white/[0.02]">
      <div className={cn("text-2xl font-semibold tracking-tight text-white", tone)}>{value}</div>
      <div className="mt-1 text-[0.68rem] uppercase tracking-[0.18em] text-slate-400">{label}</div>
      {detail && <div className="mt-2 text-xs text-slate-500">{detail}</div>}
    </div>
  );
}

function ActionButton({
  busy,
  disabled,
  kind,
  onClick,
}: {
  busy: boolean;
  disabled?: boolean;
  kind: ActionKind;
  onClick: () => void;
}) {
  const config = {
    approve: { label: "Approve", icon: Check, className: "border-emerald-200/80 bg-emerald-400 text-emerald-950 hover:bg-emerald-300" },
    reject: { label: "Reject", icon: X, className: "border-red-200/80 bg-red-500 text-white hover:bg-red-400" },
    halt: { label: "Halt", icon: PauseCircle, className: "border-amber-200/80 bg-amber-400 text-amber-950 hover:bg-amber-300" },
    undo: { label: "Undo follow-up", icon: RotateCcw, className: "border-sky-200/80 bg-sky-400 text-sky-950 hover:bg-sky-300" },
  }[kind];
  const Icon = config.icon;
  return (
    <button
      className={cn(
        "inline-flex h-9 items-center gap-1.5 rounded-full border px-3 text-xs font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70",
        config.className,
        disabled && "cursor-not-allowed opacity-45",
      )}
      disabled={disabled || busy}
      onClick={onClick}
      type="button"
    >
      {busy ? <Spinner className="text-xs" /> : <Icon className="h-3.5 w-3.5" />}
      {config.label}
    </button>
  );
}

function SourceBadge({ source }: { source: CommandCenterSource }) {
  const label = source.kind === "self_improvement" ? "self-improvement" : source.kind === "discord" ? "discord" : source.kind.replaceAll("_", " ");
  return (
    <Badge className="border-white/10 bg-white/[0.055] px-2 py-0.5 text-[0.64rem] uppercase tracking-[0.15em] text-slate-300">
      {label}
    </Badge>
  );
}

function WorkItemCard({
  activeAction,
  item,
  onAction,
  onSelect,
  selected,
}: {
  activeAction: { id: string; kind: ActionKind } | null;
  item: CommandCenterWorkItem;
  onAction: (kind: ActionKind, item: CommandCenterWorkItem) => void;
  onSelect: () => void;
  selected: boolean;
}) {
  const proposalId = item.decision?.proposal_id;
  const actionBusy = (kind: ActionKind) => activeAction?.id === item.id && activeAction.kind === kind;
  const canApproveReject = Boolean(proposalId && item.status === "proposed");
  const canHalt = Boolean(proposalId && item.execution?.task_id && ["queued", "running", "review", "blocked", "accepted"].includes(item.status));
  const canUndo = Boolean(proposalId && item.status === "shipped");
  return (
    <article
      className={cn(
        "rounded-3xl border bg-slate-950/55 p-4 shadow-2xl shadow-black/20 transition",
        selected ? "border-cyan-100/80 ring-2 ring-cyan-100/20" : "border-white/10 hover:border-cyan-100/35 hover:bg-slate-900/80",
      )}
    >
      <button className="block w-full text-left" onClick={onSelect} type="button">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <SourceBadge source={item.source} />
              <StatusPill value={item.status} />
              {item.project && <span className="text-xs uppercase tracking-[0.16em] text-slate-500">{item.project}</span>}
            </div>
            <h3 className="text-base font-semibold leading-snug text-white">{item.title}</h3>
          </div>
          <CircleDot className="mt-1 h-4 w-4 shrink-0 text-cyan-200/70" />
        </div>
        <p className="mt-3 text-sm leading-6 text-slate-300">{item.summary || item.body_preview || "No summary recorded."}</p>
      </button>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-white/10 pt-3">
        <div className="flex flex-wrap gap-2 text-xs text-slate-500">
          {item.execution?.board && <span>board: <span className="font-mono-ui text-slate-300">{item.execution.board}</span></span>}
          {item.execution?.task_id && <span>task: <span className="font-mono-ui text-slate-300">{item.execution.task_id}</span></span>}
          {item.runs?.length ? <span>{item.runs.length} run{item.runs.length === 1 ? "" : "s"}</span> : null}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          {item.execution?.worker_url && (
            <a className="inline-flex h-9 items-center gap-1.5 rounded-full border border-cyan-100/25 px-3 text-xs font-semibold text-cyan-50 hover:bg-cyan-100/10" href={item.execution.worker_url}>
              Worker <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
          {item.execution?.task_url && item.execution.task_url !== item.execution.worker_url && (
            <a className="inline-flex h-9 items-center gap-1.5 rounded-full border border-white/10 px-3 text-xs font-semibold text-slate-200 hover:bg-white/10" href={item.execution.task_url}>
              Ticket <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
          {canApproveReject && <ActionButton busy={actionBusy("approve")} kind="approve" onClick={() => onAction("approve", item)} />}
          {canApproveReject && <ActionButton busy={actionBusy("reject")} kind="reject" onClick={() => onAction("reject", item)} />}
          {canHalt && <ActionButton busy={actionBusy("halt")} kind="halt" onClick={() => onAction("halt", item)} />}
          {canUndo && <ActionButton busy={actionBusy("undo")} kind="undo" onClick={() => onAction("undo", item)} />}
        </div>
      </div>
    </article>
  );
}

function SourceCard({ source, onSelect, selected }: { source: CommandCenterSource; onSelect: () => void; selected: boolean }) {
  return (
    <button
      className={cn(
        "rounded-2xl border bg-slate-950/45 p-4 text-left transition",
        selected ? "border-cyan-100/75 ring-2 ring-cyan-100/20" : "border-white/10 hover:border-cyan-100/35",
      )}
      onClick={onSelect}
      type="button"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge className="border-white/10 bg-white/[0.055] text-slate-300">{source.label}</Badge>
        <StatusPill value={source.status} />
      </div>
      <div className="mt-3 text-sm font-semibold text-white">{source.title || source.id}</div>
      <div className="mt-2 text-xs text-slate-500">Updated {formatTime(source.updated_at || source.created_at)}</div>
      {typeof source.ref?.parse_error === "string" && (
        <p className="mt-3 line-clamp-3 text-xs leading-5 text-red-100/80">{source.ref.parse_error}</p>
      )}
    </button>
  );
}

function RunCard({ run, onSelect, selected }: { run: CommandCenterRun; onSelect: () => void; selected: boolean }) {
  const active = run.ended_at === null || run.ended_at === undefined;
  return (
    <button
      className={cn(
        "rounded-2xl border bg-slate-950/45 p-4 text-left transition",
        selected ? "border-cyan-100/75 ring-2 ring-cyan-100/20" : "border-white/10 hover:border-cyan-100/35",
      )}
      onClick={onSelect}
      type="button"
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill value={active ? "running" : run.outcome || run.status} />
        {run.board && <Badge className="border-white/10 bg-white/[0.055] text-slate-300">{run.board}</Badge>}
      </div>
      <div className="mt-3 text-sm font-semibold text-white">{run.task_title || run.task_id}</div>
      <div className="mt-2 text-xs text-slate-500">Started {formatTime(run.started_at)}{run.ended_at ? ` · Ended ${formatTime(run.ended_at)}` : ""}</div>
      {run.error && <p className="mt-3 line-clamp-3 text-xs leading-5 text-red-100/80">{run.error}</p>}
    </button>
  );
}

function DetailPanel({ selection }: { selection: Selection | null }) {
  if (!selection) {
    return (
      <Card className="sticky top-4 border-white/10 bg-white/[0.035]">
        <CardContent className="py-8 text-sm text-slate-400">Select a work item, run, or source to inspect provenance and execution links.</CardContent>
      </Card>
    );
  }
  if (selection.kind === "source") {
    const source = selection.source;
    return (
      <Card className="sticky top-4 border-white/10 bg-white/[0.045]">
        <CardHeader>
          <CardTitle className="text-base text-white">Source</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm text-slate-300">
          <div>
            <StatusPill value={source.status} />
            <h3 className="mt-3 text-lg font-semibold text-white">{source.title || source.id}</h3>
            <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">{source.label}</p>
          </div>
          <KeyValue data={source.ref} />
        </CardContent>
      </Card>
    );
  }
  if (selection.kind === "run") {
    const run = selection.run;
    return (
      <Card className="sticky top-4 border-white/10 bg-white/[0.045]">
        <CardHeader>
          <CardTitle className="text-base text-white">Worker Run</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm text-slate-300">
          <div>
            <StatusPill value={run.ended_at ? run.outcome || run.status : "running"} />
            <h3 className="mt-3 text-lg font-semibold text-white">Run #{run.id}</h3>
            <p className="mt-1 text-xs text-slate-500">{run.task_title || run.task_id}</p>
          </div>
          <KeyValue data={run as unknown as Record<string, unknown>} />
        </CardContent>
      </Card>
    );
  }
  const item = selection.item;
  return (
    <Card className="sticky top-4 border-white/10 bg-white/[0.045]">
      <CardHeader>
        <CardTitle className="text-base text-white">Work Item</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm text-slate-300">
        <div>
          <div className="flex flex-wrap gap-2"><SourceBadge source={item.source} /><StatusPill value={item.status} /></div>
          <h3 className="mt-3 text-lg font-semibold text-white">{item.title}</h3>
          <p className="mt-2 leading-6 text-slate-300">{item.summary || item.body_preview}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {item.execution?.worker_url && <a className="inline-flex items-center gap-1 rounded border border-cyan-100/25 px-3 py-1.5 text-xs text-cyan-50 hover:bg-cyan-100/10" href={item.execution.worker_url}>Worker board <ExternalLink className="h-3 w-3" /></a>}
          {item.execution?.task_url && <a className="inline-flex items-center gap-1 rounded border border-white/10 px-3 py-1.5 text-xs text-slate-200 hover:bg-white/10" href={item.execution.task_url}>Ticket <ExternalLink className="h-3 w-3" /></a>}
          {item.execution?.console_url && <Link className="inline-flex items-center gap-1 rounded border border-white/10 px-3 py-1.5 text-xs text-slate-200 hover:bg-white/10" to={item.execution.console_url}>Console <ArrowRight className="h-3 w-3" /></Link>}
        </div>
        <KeyValue data={{ source: item.source.ref, execution: item.execution, status_detail: item.status_detail }} />
        {item.source_excerpts?.length ? (
          <section>
            <h4 className="mb-2 text-xs uppercase tracking-[0.16em] text-slate-500">Evidence</h4>
            <div className="space-y-2">
              {item.source_excerpts.slice(0, 4).map((excerpt, index) => (
                <div className="rounded-xl border border-white/10 bg-black/20 p-3 text-xs leading-5 text-slate-300" key={`${excerpt.label || "excerpt"}-${index}`}>
                  <div className="mb-1 text-slate-500">{excerpt.label || "source"}</div>
                  {excerpt.text}
                </div>
              ))}
            </div>
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}

function KeyValue({ data }: { data: Record<string, unknown> | null | undefined }) {
  const entries = Object.entries(data || {}).filter(([, value]) => value !== undefined && value !== null && value !== "");
  if (!entries.length) return null;
  return (
    <dl className="grid gap-2 rounded-2xl border border-white/10 bg-black/20 p-3 text-xs">
      {entries.map(([key, value]) => (
        <div className="grid gap-1" key={key}>
          <dt className="uppercase tracking-[0.16em] text-slate-500">{key.replaceAll("_", " ")}</dt>
          <dd className="break-words font-mono-ui text-slate-300">{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function EmptyState({ label }: { label: string }) {
  return (
    <Card className="border-white/10 bg-white/[0.035]">
      <CardContent className="flex flex-col items-center gap-3 py-16 text-center text-sm text-slate-400">
        <Inbox className="h-9 w-9 text-cyan-100/75" />
        <div className="text-lg font-semibold text-white">No {label}</div>
        <p className="max-w-md leading-6">The Command Center ledger is empty for this view.</p>
      </CardContent>
    </Card>
  );
}

function WorkList({
  activeAction,
  items,
  onAction,
  onSelect,
  selectedId,
}: {
  activeAction: { id: string; kind: ActionKind } | null;
  items: CommandCenterWorkItem[];
  onAction: (kind: ActionKind, item: CommandCenterWorkItem) => void;
  onSelect: (item: CommandCenterWorkItem) => void;
  selectedId?: string;
}) {
  if (!items.length) return <EmptyState label="work items" />;
  return (
    <div className="grid gap-3">
      {items.map((item) => (
        <WorkItemCard
          activeAction={activeAction}
          item={item}
          key={item.id}
          onAction={onAction}
          onSelect={() => onSelect(item)}
          selected={selectedId === item.id}
        />
      ))}
    </div>
  );
}

export default function CommandCenterPage() {
  const location = useLocation();
  const activeView = viewFromPath(location.pathname);
  const [snapshot, setSnapshot] = useState<CommandCenterSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeAction, setActiveAction] = useState<{ id: string; kind: ActionKind } | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.getCommandCenterSnapshot({ recentRunLimitPerBoard: 25 });
      setSnapshot(next);
      setSelection((current) => {
        if (!current) {
          return initialSelectionForView(next, activeView);
        }
        if (current.kind === "work") {
          const updated = next.work_items.find((item) => item.id === current.item.id);
          return updated ? { kind: "work", item: updated } : current;
        }
        if (current.kind === "source") {
          const updated = next.sources.find((source) => source.id === current.source.id);
          return updated ? { kind: "source", source: updated } : current;
        }
        const updated = next.runs.find((run) => run.id === current.run.id && run.board === current.run.board);
        return updated ? { kind: "run", run: updated } : current;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [activeView]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async snapshot load mirrors existing dashboard data pages.
    void refresh();
  }, [refresh]);

  const inboxItems = useMemo(
    () => snapshot?.work_items.filter(isInboxItem) ?? [],
    [snapshot],
  );
  const inboxSources = useMemo(
    () => snapshot?.sources.filter((source) => source.bucket === "inbox") ?? [],
    [snapshot],
  );
  const workItems = useMemo(
    () => snapshot?.work_items.filter(isWorkItem) ?? [],
    [snapshot],
  );
  const overviewItems = useMemo(() => {
    const seen = new Set<string>();
    return [...inboxItems, ...workItems].filter((item) => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    });
  }, [inboxItems, workItems]);
  const recommendations = useMemo(
    () => snapshot?.work_items.filter((item) => item.source.kind === "self_improvement") ?? [],
    [snapshot],
  );

  const handleAction = useCallback(async (kind: ActionKind, item: CommandCenterWorkItem) => {
    const proposalId = item.decision?.proposal_id;
    if (!proposalId) return;
    setActiveAction({ id: item.id, kind });
    setError(null);
    try {
      if (kind === "approve") {
        await api.approveSelfImprovementProposal(proposalId);
      } else if (kind === "reject") {
        const reason = window.prompt("Reject reason for future prong feedback?", "Not worth doing right now.");
        if (!reason) return;
        await api.rejectSelfImprovementProposal(proposalId, reason);
      } else if (kind === "halt") {
        const reason = window.prompt("Reason to halt downstream work?", "Operator halted from Command Center.") || undefined;
        await api.haltSelfImprovementProposal(proposalId, reason);
      } else if (kind === "undo") {
        const reason = window.prompt("Reason for undo follow-up?", "Operator requested undo follow-up from Command Center.") || undefined;
        await api.requestSelfImprovementUndoFollowup(proposalId, reason);
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setActiveAction(null);
    }
  }, [refresh]);

  const selectedWorkId = selection?.kind === "work" ? selection.item.id : undefined;
  const selectedSourceId = selection?.kind === "source" ? selection.source.id : undefined;
  const selectedRunId = selection?.kind === "run" ? `${selection.run.board || "default"}:${selection.run.id}` : undefined;

  return (
    <div className="flex flex-col gap-5">
      <section className="relative overflow-hidden rounded-3xl border border-cyan-300/20 bg-[radial-gradient(circle_at_top_left,rgba(34,211,238,0.18),transparent_34%),linear-gradient(135deg,rgba(8,47,73,0.88),rgba(2,6,23,0.96))] p-5 shadow-2xl shadow-cyan-950/30 sm:p-7">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
          <div className="max-w-4xl">
            <Badge className="border-cyan-200/30 bg-cyan-200/10 px-2.5 py-1 text-[0.68rem] uppercase tracking-[0.18em] text-cyan-100">
              Command Center
            </Badge>
            <h1 className="mt-4 text-3xl font-semibold tracking-tight text-white sm:text-4xl">
              One ledger for sources, work items, and worker runs.
            </h1>
            <p className="mt-3 max-w-3xl text-sm leading-6 text-cyan-50/75">
              Self-improvement and Discord are intake sources. Worker boards are execution runs. This surface keeps the parent Work Item coherent while preserving the existing Kanban and public worker pages.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button className="border-cyan-100/25 bg-cyan-100 text-slate-950 hover:bg-cyan-50" disabled={loading} onClick={() => void refresh()}>
              {loading ? <Spinner className="mr-2 text-xs" /> : <RefreshCw className="mr-2 h-4 w-4" />} Refresh
            </Button>
            <a className="inline-flex items-center rounded-md border border-cyan-100/25 px-4 py-2 text-sm font-medium text-cyan-50 hover:bg-cyan-100/10" href="/workers">
              Legacy Workers <ExternalLink className="ml-2 h-4 w-4" />
            </a>
          </div>
        </div>
      </section>

      <nav aria-label="Command Center views" className="flex flex-wrap gap-1 rounded-full border border-white/10 bg-white/[0.035] p-1">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          const active = activeView === tab.key;
          return (
            <NavLink
              className={cn(
                "inline-flex items-center gap-2 rounded-full px-3 py-2 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan-200/70",
                active ? "bg-cyan-100 text-slate-950" : "text-slate-300 hover:bg-white/10 hover:text-white",
              )}
              key={tab.key}
              to={tab.path}
            >
              <Icon className="h-4 w-4" /> {tab.label}
            </NavLink>
          );
        })}
      </nav>

      <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricCard label="Inbox" value={metric(snapshot, "inbox")} tone="text-cyan-100" />
        <MetricCard label="Active work" value={metric(snapshot, "active_work")} tone="text-amber-100" />
        <MetricCard label="Recommendations" value={metric(snapshot, "recommendations")} tone="text-fuchsia-100" />
        <MetricCard label="Discord-origin" value={metric(snapshot, "discord_origin")} tone="text-indigo-100" />
        <MetricCard label="Active runs" value={metric(snapshot, "active_runs")} tone="text-emerald-100" />
        <MetricCard label="Parse failures" value={metric(snapshot, "parse_failures")} tone="text-red-100" />
      </div>

      {error && (
        <Card className="border-red-300/30 bg-red-950/30">
          <CardContent className="flex items-start gap-3 py-4 text-sm text-red-100">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </CardContent>
        </Card>
      )}

      {loading && !snapshot ? (
        <Card className="border-white/10 bg-white/[0.035]">
          <CardContent className="flex items-center justify-center gap-3 py-20 text-slate-300">
            <Spinner /> Loading Command Center…
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_25rem]">
          <section className="min-w-0">
            {activeView === "overview" && (
              <div className="grid gap-5">
                <Card className="border-white/10 bg-white/[0.035]">
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2 text-lg text-white"><GitBranch className="h-5 w-5 text-cyan-200" /> Operating model</CardTitle>
                  </CardHeader>
                  <CardContent className="grid gap-3 text-sm text-slate-300 md:grid-cols-3">
                    <div className="rounded-2xl border border-white/10 bg-black/20 p-4"><Sparkles className="mb-3 h-5 w-5 text-cyan-200" /><strong className="text-white">Sources</strong><p className="mt-2 leading-6">Self-improvement, Discord, crons, and manual entries create or attach to work.</p></div>
                    <div className="rounded-2xl border border-white/10 bg-black/20 p-4"><ListChecks className="mb-3 h-5 w-5 text-cyan-200" /><strong className="text-white">Work Items</strong><p className="mt-2 leading-6">One parent object owns status, provenance, decision state, and execution links.</p></div>
                    <div className="rounded-2xl border border-white/10 bg-black/20 p-4"><Wrench className="mb-3 h-5 w-5 text-cyan-200" /><strong className="text-white">Runs</strong><p className="mt-2 leading-6">Workers and Kanban attempts are execution detail, not competing boards.</p></div>
                  </CardContent>
                </Card>
                <WorkList activeAction={activeAction} items={overviewItems.slice(0, 10)} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />
              </div>
            )}
            {activeView === "inbox" && (
              <div className="grid gap-4">
                <WorkList activeAction={activeAction} items={inboxItems} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />
                {inboxSources.map((source) => <SourceCard key={source.id} onSelect={() => setSelection({ kind: "source", source })} selected={selectedSourceId === source.id} source={source} />)}
              </div>
            )}
            {activeView === "work" && <WorkList activeAction={activeAction} items={workItems} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />}
            {activeView === "recommendations" && <WorkList activeAction={activeAction} items={recommendations} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />}
            {activeView === "runs" && (
              <div className="grid gap-3">
                {snapshot?.runs.length ? snapshot.runs.map((run) => (
                  <RunCard key={`${run.board || "default"}:${run.id}`} onSelect={() => setSelection({ kind: "run", run })} run={run} selected={selectedRunId === `${run.board || "default"}:${run.id}`} />
                )) : <EmptyState label="worker runs" />}
              </div>
            )}
            {activeView === "sources" && (
              <div className="grid gap-3">
                {snapshot?.sources.length ? snapshot.sources.map((source) => (
                  <SourceCard key={source.id} onSelect={() => setSelection({ kind: "source", source })} selected={selectedSourceId === source.id} source={source} />
                )) : <EmptyState label="sources" />}
              </div>
            )}
          </section>
          <aside className="min-w-0">
            <DetailPanel selection={selection} />
          </aside>
        </div>
      )}

      <Card className="border-white/10 bg-white/[0.035]">
        <CardContent className="flex flex-col gap-3 py-4 text-xs leading-5 text-slate-500 sm:flex-row sm:items-center sm:justify-between">
          <span>{snapshot?.summary || "Sources create Work Items; workers execute them."}</span>
          <span>Generated {formatTime(snapshot?.generated_at)} · <Send className="inline h-3 w-3" /> worker work from Discord is included as Discord-origin work.</span>
        </CardContent>
      </Card>
    </div>
  );
}
