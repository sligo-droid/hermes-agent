import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  ArrowRight,
  Check,
  CircleDot,
  ExternalLink,
  Inbox,
  PauseCircle,
  RotateCcw,
  PlayCircle,
  Send,
  X,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
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

type ViewKey = "overview" | "inbox" | "work" | "archive" | "runs" | "recommendations" | "sources";
type Selection =
  | { kind: "work"; item: CommandCenterWorkItem }
  | { kind: "source"; source: CommandCenterSource }
  | { kind: "run"; run: CommandCenterRun };

type ActionKind = "approve" | "reject" | "pause" | "resume" | "undo" | "archive";

declare global {
  interface Window {
    __commandCenterRefresh?: () => void;
  }
}

function viewFromPath(pathname: string): ViewKey {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  if (normalized.includes("/inbox")) return "inbox";
  if (normalized.includes("/work")) return "work";
  if (normalized.includes("/archive")) return "archive";
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
  return Boolean(item.decision?.needed) || item.status === "proposed";
}

function isWorkItem(item: CommandCenterWorkItem): boolean {
  return item.status !== "proposed" && item.status !== "rejected" && item.status !== "archived";
}

function isArchivedItem(item: CommandCenterWorkItem): boolean {
  return item.status === "archived" || item.status === "rejected";
}

function isRunningWorkItem(item: CommandCenterWorkItem): boolean {
  return item.status === "running" || Boolean(item.execution?.active_run_id) || Boolean(item.runs?.some(runIsActive));
}

function workItemViewSort(a: CommandCenterWorkItem, b: CommandCenterWorkItem): number {
  const runningDelta = Number(isRunningWorkItem(b)) - Number(isRunningWorkItem(a));
  if (runningDelta) return runningDelta;
  const pausedDelta = Number(b.status === "paused") - Number(a.status === "paused");
  if (pausedDelta) return pausedDelta;
  return 0;
}

function runIsActive(run: CommandCenterRun): boolean {
  return !run.ended_at && run.task_status === "running";
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
  if (activeView === "archive") {
    const item = snapshot.work_items.find(isArchivedItem);
    return item ? { kind: "work", item } : null;
  }
  const item = snapshot.work_items[0];
  if (item) return { kind: "work", item };
  const source = snapshot.sources[0];
  if (source) return { kind: "source", source };
  const run = snapshot.runs[0];
  return run ? { kind: "run", run } : null;
}

function WorkStatePanel({
  activeView,
  laneCounts,
}: {
  activeView: ViewKey;
  laneCounts: { overview: number; inbox: number; work: number; archive: number; workers: number };
}) {
  const lanes = [
    { key: "overview", label: "Overview", href: "/sligo", value: laneCounts.overview, detail: "open ledger" },
    { key: "inbox", label: "Inbox", href: "/sligo/inbox", value: laneCounts.inbox, detail: "needs decision" },
    { key: "work", label: "Work", href: "/sligo/work", value: laneCounts.work, detail: "accepted / active" },
    { key: "archive", label: "Archive", href: "/sligo/archive", value: laneCounts.archive, detail: "terminal / hidden" },
    { key: "workers", label: "Workers", href: "/workers", value: laneCounts.workers, detail: "opens monitor", external: true },
  ];
  const tileClass = (selected: boolean) => cn(
    "group rounded-2xl border px-3.5 py-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-100/40",
    selected ? "border-cyan-100/55 bg-cyan-100/10 text-cyan-50" : "border-white/10 bg-white/[0.035] text-slate-300 hover:border-cyan-100/35 hover:bg-cyan-100/[0.055]",
  );
  return (
    <Card className="border-white/10 bg-white/[0.035]">
      <CardHeader className="gap-1">
        <CardTitle className="text-base text-white">Work State</CardTitle>
        <p className="text-xs leading-5 text-slate-500">Use this as the Command Center map: lanes move between views, and Workers opens the execution monitor in a new tab.</p>
      </CardHeader>
      <CardContent>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5" aria-label="Command Center lanes">
          {lanes.map((lane) => {
            const selected = !lane.external && activeView === lane.key;
            const content = (
              <>
                <span className="block text-xl font-semibold tracking-tight text-white">{lane.value}</span>
                <span className="mt-1 block text-[0.68rem] font-semibold uppercase tracking-[0.16em]">{lane.label}</span>
                <span className="mt-1 block text-xs text-slate-500 transition group-hover:text-slate-400">{lane.detail}</span>
              </>
            );
            if (lane.external) {
              return (
                <a className={tileClass(selected)} href={lane.href} key={lane.key} rel="noopener noreferrer" target="_blank">
                  {content}
                  <span className="sr-only">opens in a new tab</span>
                </a>
              );
            }
            return (
              <Link className={tileClass(selected)} key={lane.key} to={lane.href}>
                {content}
              </Link>
            );
          })}
        </div>
      </CardContent>
    </Card>
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
  const tooltipId = useId();
  const config = {
    approve: { label: "Approve", icon: Check, className: "border-emerald-200/70 bg-emerald-400 text-emerald-950 hover:bg-emerald-300 focus-visible:ring-emerald-100/75" },
    reject: { label: "Reject", icon: X, className: "border-red-200/75 bg-red-500 text-white hover:bg-red-400 focus-visible:ring-red-100/75", strong: true },
    pause: { label: "Pause", icon: PauseCircle, className: "border-orange-200/70 bg-orange-400 text-orange-950 hover:bg-orange-300 focus-visible:ring-orange-100/75", strong: true },
    resume: { label: "Resume", icon: PlayCircle, className: "border-emerald-200/70 bg-emerald-400 text-emerald-950 hover:bg-emerald-300 focus-visible:ring-emerald-100/75", strong: true },
    undo: { label: "Revert", icon: RotateCcw, className: "border-sky-200/65 bg-sky-400 text-sky-950 hover:bg-sky-300 focus-visible:ring-sky-100/75" },
    archive: { label: "Archive board", icon: Archive, className: "border-violet-200/60 bg-violet-400 text-violet-950 hover:bg-violet-300 focus-visible:ring-violet-100/75" },
  }[kind];
  const Icon = config.icon;
  return (
    <span className="group/action relative inline-flex">
      <button
        aria-describedby={tooltipId}
        aria-label={config.label}
        className={cn(
          "inline-flex h-10 w-10 items-center justify-center rounded-full border text-xs font-semibold shadow-sm shadow-black/20 transition hover:shadow-black/35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950 disabled:cursor-not-allowed disabled:border-slate-500/25 disabled:bg-slate-700/35 disabled:text-slate-400 disabled:opacity-70 disabled:shadow-none",
          config.className,
        )}
        disabled={disabled || busy}
        onClick={onClick}
        type="button"
      >
        <Icon className={cn(config.strong ? "h-6 w-6 stroke-[2.35]" : "h-5 w-5 stroke-[2.15]", busy && "animate-pulse")} />
        <span className="sr-only">{config.label}</span>
      </button>
      <span id={tooltipId} role="tooltip" className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-2 -translate-x-1/2 translate-y-1 whitespace-nowrap rounded-md border border-white/15 bg-[#090a0c]/95 px-2.5 py-1 text-[0.68rem] font-medium text-slate-100 opacity-0 shadow-xl shadow-black/35 transition duration-150 group-hover/action:translate-y-0 group-hover/action:opacity-100 group-focus-within/action:translate-y-0 group-focus-within/action:opacity-100">
        {config.label}
      </span>
    </span>
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
  const proposalCanArchive = Boolean(proposalId && ["queued", "running", "review", "blocked", "accepted", "paused"].includes(item.status));
  const canPause = Boolean(["queued", "running", "review", "accepted"].includes(item.status) && (proposalId || (item.execution?.pause_action && item.execution.board)) && !item.execution?.paused);
  const canResume = Boolean((item.status === "paused" || item.execution?.paused || item.execution?.resumable) && (proposalId || (item.execution?.resume_action && item.execution.board)) && item.status !== "archived");
  const canUndo = Boolean(proposalId && item.status === "shipped");
  const canArchive = Boolean((item.execution?.archiveable && item.execution.board && item.execution.board !== "default" && item.id.startsWith("kanban-board:")) || proposalCanArchive);
  return (
    <article
      className={cn(
        "rounded-2xl border bg-[#08090a]/80 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.025)] transition",
        selected ? "border-cyan-100/55 bg-cyan-100/[0.03] ring-1 ring-cyan-100/15" : "border-white/10 hover:border-white/20 hover:bg-white/[0.03]",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <button className="min-w-0 flex-1 text-left" onClick={onSelect} type="button">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <SourceBadge source={item.source} />
            <StatusPill value={item.status} />
            {item.project && <span className="text-xs uppercase tracking-[0.16em] text-slate-500">{item.project}</span>}
          </div>
          <h3 className="text-base font-semibold leading-snug text-white">{item.title}</h3>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          {item.execution?.worker_url ? (
            <a className="inline-flex h-8 items-center gap-1.5 rounded-full border border-cyan-100/25 px-2.5 text-xs font-semibold text-cyan-50 transition hover:border-cyan-100/40 hover:bg-cyan-100/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-100/35" href={item.execution.worker_url} rel="noopener noreferrer" target="_blank">
              Worker <ExternalLink className="h-3 w-3" /><span className="sr-only">opens in a new tab</span>
            </a>
          ) : (
            <CircleDot className="mt-1 h-4 w-4 text-cyan-200/70" />
          )}
        </div>
      </div>
      <button className="mt-3 block w-full text-left" onClick={onSelect} type="button">
        <p className="text-sm leading-6 text-slate-300">{item.summary || item.body_preview || "No summary yet."}</p>
      </button>
      {(item.execution?.task_url && item.execution.task_url !== item.execution.worker_url) || canApproveReject || canArchive || canPause || canResume || canUndo ? (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-white/[0.08] pt-3">
          {item.execution?.task_url && item.execution.task_url !== item.execution.worker_url && (
            <a className="inline-flex h-9 items-center gap-1.5 rounded-full border border-white/10 px-3 text-xs font-semibold text-slate-200 transition hover:border-white/20 hover:bg-white/[0.07] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/20" href={item.execution.task_url} rel="noopener noreferrer" target="_blank">
              Ticket <ExternalLink className="h-3.5 w-3.5" /><span className="sr-only">opens in a new tab</span>
            </a>
          )}
          {canApproveReject && <ActionButton busy={actionBusy("approve")} kind="approve" onClick={() => onAction("approve", item)} />}
          {canApproveReject && <ActionButton busy={actionBusy("reject")} kind="reject" onClick={() => onAction("reject", item)} />}
          {canArchive && <ActionButton busy={actionBusy("archive")} kind="archive" onClick={() => onAction("archive", item)} />}
          {canResume && <ActionButton busy={actionBusy("resume")} kind="resume" onClick={() => onAction("resume", item)} />}
          {canPause && <ActionButton busy={actionBusy("pause")} kind="pause" onClick={() => onAction("pause", item)} />}
          {canUndo && <ActionButton busy={actionBusy("undo")} kind="undo" onClick={() => onAction("undo", item)} />}
        </div>
      ) : null}
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
  const active = runIsActive(run);
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
            <StatusPill value={runIsActive(run) ? "running" : run.outcome || run.status} />
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
          {item.execution?.worker_url && <a className="inline-flex items-center gap-1 rounded border border-cyan-100/25 px-3 py-1.5 text-xs text-cyan-50 hover:bg-cyan-100/10" href={item.execution.worker_url} rel="noopener noreferrer" target="_blank">Worker board <ExternalLink className="h-3 w-3" /><span className="sr-only">opens in a new tab</span></a>}
          {item.execution?.task_url && <a className="inline-flex items-center gap-1 rounded border border-white/10 px-3 py-1.5 text-xs text-slate-200 hover:bg-white/10" href={item.execution.task_url} rel="noopener noreferrer" target="_blank">Ticket <ExternalLink className="h-3 w-3" /><span className="sr-only">opens in a new tab</span></a>}
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

function EmptyState({ label, message }: { label: string; message?: string }) {
  return (
    <Card className="border-white/10 bg-white/[0.035]">
      <CardContent className="flex flex-col items-center gap-3 py-16 text-center text-sm text-slate-400">
        <Inbox className="h-9 w-9 text-cyan-100/75" />
        <div className="text-lg font-semibold text-white">No {label}</div>
        <p className="max-w-md leading-6">{message || "Nothing is waiting in this view."}</p>
      </CardContent>
    </Card>
  );
}

function WorkList({
  activeAction,
  emptyLabel,
  emptyMessage,
  items,
  onAction,
  onSelect,
  selectedId,
}: {
  activeAction: { id: string; kind: ActionKind } | null;
  emptyLabel?: string;
  emptyMessage?: string;
  items: CommandCenterWorkItem[];
  onAction: (kind: ActionKind, item: CommandCenterWorkItem) => void;
  onSelect: (item: CommandCenterWorkItem) => void;
  selectedId?: string;
}) {
  if (!items.length) return <EmptyState label={emptyLabel || "work items"} message={emptyMessage} />;
  return (
    <div className="grid gap-3">
      {[...items].sort(workItemViewSort).map((item) => (
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

function OverviewWorkList({
  activeAction,
  emptyMessage,
  items,
  onAction,
  onSelect,
  selectedId,
}: {
  activeAction: { id: string; kind: ActionKind } | null;
  emptyMessage?: string;
  items: CommandCenterWorkItem[];
  onAction: (kind: ActionKind, item: CommandCenterWorkItem) => void;
  onSelect: (item: CommandCenterWorkItem) => void;
  selectedId?: string;
}) {
  if (!items.length) return <EmptyState label="work items" message={emptyMessage} />;

  const runningItems = items.filter(isRunningWorkItem);
  const remainingItems = items.filter((item) => !isRunningWorkItem(item));
  const renderItem = (item: CommandCenterWorkItem) => (
    <WorkItemCard
      activeAction={activeAction}
      item={item}
      key={item.id}
      onAction={onAction}
      onSelect={() => onSelect(item)}
      selected={selectedId === item.id}
    />
  );

  return (
    <div className="grid gap-3">
      {runningItems.map(renderItem)}
      {runningItems.length > 0 && remainingItems.length > 0 ? (
        <div className="flex items-center gap-3 py-1.5" aria-hidden="true">
          <div className="h-px flex-1 bg-gradient-to-r from-transparent via-white/12 to-transparent" />
          <span className="text-[0.62rem] font-medium uppercase tracking-[0.18em] text-slate-500">Proposed and parked</span>
          <div className="h-px flex-1 bg-gradient-to-r from-transparent via-white/12 to-transparent" />
        </div>
      ) : null}
      {remainingItems.map(renderItem)}
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
      const next = await api.getCommandCenterSnapshot({ includeArchived: true, recentRunLimitPerBoard: 25 });
      setSnapshot(next);
      setSelection((current) => {
        if (!current) {
          return initialSelectionForView(next, activeView);
        }
        if (current.kind === "work") {
          const updated = next.work_items.find((item) => item.id === current.item.id);
          return updated ? { kind: "work", item: updated } : initialSelectionForView(next, activeView);
        }
        if (current.kind === "source") {
          const updated = next.sources.find((source) => source.id === current.source.id);
          return updated ? { kind: "source", source: updated } : initialSelectionForView(next, activeView);
        }
        const updated = next.runs.find((run) => run.id === current.run.id && run.board === current.run.board);
        return updated ? { kind: "run", run: updated } : initialSelectionForView(next, activeView);
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

  useEffect(() => {
    const invokeRefresh = () => {
      void refresh();
    };
    window.__commandCenterRefresh = invokeRefresh;
    const refreshFromShell = () => {
      void refresh();
    };
    window.addEventListener("command-center:refresh", refreshFromShell);
    return () => {
      if (window.__commandCenterRefresh === invokeRefresh) {
        delete window.__commandCenterRefresh;
      }
      window.removeEventListener("command-center:refresh", refreshFromShell);
    };
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
  const archivedItems = useMemo(
    () => snapshot?.work_items.filter(isArchivedItem) ?? [],
    [snapshot],
  );
  const overviewItems = useMemo(() => {
    const seen = new Set<string>();
    const merged = [...inboxItems, ...workItems].filter((item) => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    });
    return merged.sort(workItemViewSort);
  }, [inboxItems, workItems]);
  const recommendations = useMemo(
    () => snapshot?.work_items.filter((item) => item.source.kind === "self_improvement") ?? [],
    [snapshot],
  );
  const sources = useMemo(() => snapshot?.sources ?? [], [snapshot]);
  const laneCounts = useMemo(() => ({
    overview: overviewItems.length,
    inbox: inboxItems.length + inboxSources.length,
    work: workItems.length,
    archive: archivedItems.length,
    workers: metric(snapshot, "active_runs"),
  }), [archivedItems.length, inboxItems.length, inboxSources.length, overviewItems.length, snapshot, workItems.length]);
  const activeViewWorkItems = useMemo(() => {
    if (activeView === "overview") return overviewItems;
    if (activeView === "inbox") return inboxItems;
    if (activeView === "work") return workItems;
    if (activeView === "archive") return archivedItems;
    if (activeView === "recommendations") return recommendations;
    return [];
  }, [activeView, archivedItems, inboxItems, overviewItems, recommendations, workItems]);
  const activeViewSources = useMemo(() => {
    if (activeView === "inbox") return inboxSources;
    if (activeView === "sources") return sources;
    return [];
  }, [activeView, inboxSources, sources]);
  const activeViewRuns = useMemo(() => activeView === "runs" ? (snapshot?.runs ?? []) : [], [activeView, snapshot]);

  useEffect(() => {
    if (!snapshot || loading) return;
    setSelection((current) => {
      if (current?.kind === "work" && activeViewWorkItems.some((item) => item.id === current.item.id)) return current;
      if (current?.kind === "source" && activeViewSources.some((source) => source.id === current.source.id)) return current;
      if (current?.kind === "run" && activeViewRuns.some((run) => run.id === current.run.id && run.board === current.run.board)) return current;
      const item = activeViewWorkItems[0];
      if (item) return { kind: "work", item };
      const source = activeViewSources[0];
      if (source) return { kind: "source", source };
      const run = activeViewRuns[0];
      return run ? { kind: "run", run } : null;
    });
  }, [activeViewRuns, activeViewSources, activeViewWorkItems, loading, snapshot]);

  const handleAction = useCallback(async (kind: ActionKind, item: CommandCenterWorkItem) => {
    const proposalId = item.decision?.proposal_id;
    const board = item.execution?.board;
    if (kind === "archive" && !proposalId && (!item.execution?.archiveable || !board || board === "default")) return;
    if (["approve", "reject", "undo"].includes(kind) && !proposalId) return;
    if (["pause", "resume"].includes(kind) && !proposalId && (!board || board === "default")) return;
    setActiveAction({ id: item.id, kind });
    setError(null);
    try {
      if (kind === "archive") {
        if (board && board !== "default") await api.archiveKanbanBoard(board);
        else if (proposalId) await api.haltSelfImprovementProposal(proposalId);
      } else if (proposalId && kind === "approve") {
        await api.approveSelfImprovementProposal(proposalId);
      } else if (proposalId && kind === "reject") {
        const reason = window.prompt("Reject reason for future prong feedback?", "Not worth doing right now.");
        if (!reason) return;
        await api.rejectSelfImprovementProposal(proposalId, reason);
      } else if (kind === "pause") {
        if (proposalId) await api.pauseSelfImprovementProposal(proposalId);
        else if (board) await api.pauseKanbanBoard(board);
      } else if (kind === "resume") {
        if (proposalId) await api.resumeSelfImprovementProposal(proposalId);
        else if (board) await api.resumeKanbanBoard(board);
      } else if (proposalId && kind === "undo") {
        const reason = window.prompt("Reason for revert follow-up?", "Operator requested revert follow-up from Command Center.") || undefined;
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
      <WorkStatePanel
        activeView={activeView}
        laneCounts={laneCounts}
      />

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
              <OverviewWorkList activeAction={activeAction} emptyMessage="No recent decisions, worker boards, or active work yet." items={overviewItems} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />
            )}
            {activeView === "inbox" && (
              <div className="grid gap-4">
                <WorkList activeAction={activeAction} emptyLabel="pending decisions" emptyMessage="Inbox is clear. Finished, blocked, and archiveable boards stay on Overview or Work." items={inboxItems} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />
                {inboxSources.map((source) => <SourceCard key={source.id} onSelect={() => setSelection({ kind: "source", source })} selected={selectedSourceId === source.id} source={source} />)}
              </div>
            )}
            {activeView === "work" && <WorkList activeAction={activeAction} emptyMessage="No active or recently shipped work is visible." items={workItems} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />}
            {activeView === "archive" && <WorkList activeAction={activeAction} emptyLabel="archived items" emptyMessage="Archived worker boards and work items will appear here." items={archivedItems} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />}
            {activeView === "recommendations" && <WorkList activeAction={activeAction} emptyLabel="recommendations" emptyMessage="No self-improvement recommendations are waiting." items={recommendations} onAction={handleAction} onSelect={(item) => setSelection({ kind: "work", item })} selectedId={selectedWorkId} />}
            {activeView === "runs" && (
              <div className="grid gap-3">
                {snapshot?.runs.length ? snapshot.runs.map((run) => (
                  <RunCard key={`${run.board || "default"}:${run.id}`} onSelect={() => setSelection({ kind: "run", run })} run={run} selected={selectedRunId === `${run.board || "default"}:${run.id}`} />
                )) : <EmptyState label="worker runs" />}
              </div>
            )}
            {activeView === "sources" && (
              <div className="grid gap-3">
                {sources.length ? sources.map((source) => (
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
          <span>{snapshot?.summary || "Sources create work items; workers move them forward."}</span>
          <span>Generated {formatTime(snapshot?.generated_at)} · <Send className="inline h-3 w-3" /> Worker-board work rolls up board-level execution.</span>
        </CardContent>
      </Card>
    </div>
  );
}
