import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  Check,
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
  CommandCenterProject,
  CommandCenterRun,
  CommandCenterSnapshot,
  CommandCenterSource,
  CommandCenterWorkItem,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type ViewKey = "overview" | "inbox" | "work" | "archive" | "runs" | "recommendations" | "sources";
type ActionKind = "approve" | "reject" | "pause" | "resume" | "undo" | "archive";
type ActiveAction = { ids: string[]; kind: ActionKind };

const ACTION_SETTLE_MS = 600;
const ACTION_PROGRESS_LABELS: Record<ActionKind, string> = {
  approve: "Approving",
  reject: "Rejecting",
  pause: "Pausing",
  resume: "Resuming",
  undo: "Requesting revert",
  archive: "Archiving",
};

function waitForActionSettle(durationMs = ACTION_SETTLE_MS): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, durationMs));
}

declare global {
  interface Window {
    __commandCenterRefresh?: () => Promise<void> | void;
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

function workItemRecency(item: CommandCenterWorkItem): number {
  const value = item.created_at || item.updated_at;
  if (value === null || value === undefined || value === "") return 0;
  const timestamp = typeof value === "number" ? value * 1000 : new Date(value).getTime();
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function workItemCreatedSort(a: CommandCenterWorkItem, b: CommandCenterWorkItem): number {
  const recencyDelta = workItemRecency(b) - workItemRecency(a);
  if (recencyDelta) return recencyDelta;
  return a.id.localeCompare(b.id);
}

function workItemViewSort(a: CommandCenterWorkItem, b: CommandCenterWorkItem): number {
  const runningDelta = Number(isRunningWorkItem(b)) - Number(isRunningWorkItem(a));
  if (runningDelta) return runningDelta;
  return workItemCreatedSort(a, b);
}

function runIsActive(run: CommandCenterRun): boolean {
  return !run.ended_at && run.task_status === "running";
}

function availableActionKinds(item: CommandCenterWorkItem): ActionKind[] {
  const proposalId = item.decision?.proposal_id;
  const proposalCanArchive = Boolean(proposalId && ["queued", "running", "review", "blocked", "accepted", "paused"].includes(item.status));
  const canApproveReject = Boolean(proposalId && item.status === "proposed");
  const canPause = Boolean(["queued", "running", "review", "accepted"].includes(item.status) && (proposalId || (item.execution?.pause_action && item.execution.board)) && !item.execution?.paused);
  const canResume = Boolean((item.status === "paused" || item.execution?.paused || item.execution?.resumable) && (proposalId || (item.execution?.resume_action && item.execution.board)) && item.status !== "archived");
  const canUndo = Boolean(proposalId && item.status === "shipped");
  const canArchive = Boolean((item.execution?.archiveable && item.execution.board && item.execution.board !== "default" && item.id.startsWith("kanban-board:")) || proposalCanArchive);
  const actions: ActionKind[] = [];
  if (canApproveReject) actions.push("approve", "reject");
  if (canResume) actions.push("resume");
  if (canPause) actions.push("pause");
  if (canUndo) actions.push("undo");
  if (canArchive) actions.push("archive");
  return actions;
}

function actionSet(items: CommandCenterWorkItem[], mode: "union" | "common"): Set<ActionKind> {
  if (!items.length) return new Set();
  const sets = items.map((item) => new Set(availableActionKinds(item)));
  if (mode === "union") return new Set(sets.flatMap((set) => [...set]));
  return new Set([...sets[0]].filter((kind) => sets.every((set) => set.has(kind))));
}

function WorkStatePanel({
  activeView,
  laneCounts,
  search,
}: {
  activeView: ViewKey;
  laneCounts: { overview: number; inbox: number; work: number; archive: number; workers: number };
  search: string;
}) {
  const lanes = [
    { key: "overview", label: "Overview", href: "/sligo", value: laneCounts.overview, detail: "open ledger" },
    { key: "inbox", label: "Inbox", href: "/sligo/inbox", value: laneCounts.inbox, detail: "needs decision" },
    { key: "work", label: "Work", href: "/sligo/work", value: laneCounts.work, detail: "accepted / active" },
    { key: "archive", label: "Archive", href: "/sligo/archive", value: laneCounts.archive, detail: "terminal / hidden" },
    { key: "workers", label: "Workers", href: "/workers", value: laneCounts.workers, detail: "opens monitor", external: true },
  ];
  const tileClass = (selected: boolean) => cn(
    "command-center-lane group rounded-2xl border px-3.5 py-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-100/40",
    selected ? "command-center-lane-selected border-cyan-100/55 bg-cyan-100/10 text-cyan-50" : "border-white/10 bg-white/[0.035] text-slate-300 hover:border-cyan-100/35 hover:bg-cyan-100/[0.055]",
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
                <span className="command-center-lane-value block text-xl font-semibold tracking-tight text-white">{lane.value}</span>
                <span className="mt-1 block text-[0.68rem] font-semibold uppercase tracking-[0.16em]">{lane.label}</span>
                <span className="command-center-lane-detail mt-1 block text-xs text-slate-500 transition group-hover:text-slate-400">{lane.detail}</span>
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
              <Link className={tileClass(selected)} key={lane.key} to={{ pathname: lane.href, search }}>
                {content}
              </Link>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

function ProjectTabs({
  currentProject,
  pathname,
  projects,
}: {
  currentProject: string | null;
  pathname: string;
  projects: CommandCenterProject[];
}) {
  if (!projects.length) return null;
  const tabSearch = (project: string) => {
    const params = new URLSearchParams();
    params.set("project", project);
    return `?${params.toString()}`;
  };
  return (
    <nav aria-label="Command Center projects" className="command-center-project-tabs flex border-b border-white/10">
      {projects.map((project) => {
        const selected = currentProject === project.key;
        return (
          <Link
            aria-current={selected ? "page" : undefined}
            className={cn(
              "command-center-project-tab relative -mb-px border-b-2 px-4 py-2 text-sm font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-100/40",
              selected
                ? "command-center-project-tab-selected border-cyan-200 bg-cyan-100/[0.08] text-cyan-50"
                : "border-transparent text-slate-400 hover:border-cyan-100/35 hover:bg-white/[0.035] hover:text-slate-100",
            )}
            key={project.key}
            to={{ pathname, search: tabSearch(project.key) }}
          >
            <span>{project.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}

function ActionButton({
  busy,
  disabled,
  kind,
  onClick,
  title,
}: {
  busy: boolean;
  disabled?: boolean;
  kind: ActionKind;
  onClick: () => void;
  title?: string;
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
          "command-center-action-button inline-flex h-10 w-10 items-center justify-center rounded-full border text-xs font-semibold shadow-sm shadow-black/20 transition hover:shadow-black/35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950 disabled:cursor-not-allowed disabled:border-slate-500/25 disabled:bg-slate-700/35 disabled:text-slate-400 disabled:opacity-70 disabled:shadow-none",
          config.className,
        )}
        disabled={disabled || busy}
        onClick={(event) => {
          event.stopPropagation();
          onClick();
        }}
        type="button"
        title={title || config.label}
      >
        <Icon className={cn(config.strong ? "h-6 w-6 stroke-[2.35]" : "h-5 w-5 stroke-[2.15]", busy && "animate-pulse")} />
        <span className="sr-only">{config.label}</span>
      </button>
      <span id={tooltipId} role="tooltip" className="command-center-action-tooltip pointer-events-none absolute bottom-full left-1/2 z-20 mb-2 -translate-x-1/2 translate-y-1 whitespace-nowrap rounded-md border border-white/15 bg-[#090a0c]/95 px-2.5 py-1 text-[0.68rem] font-medium text-slate-100 opacity-0 shadow-xl shadow-black/35 transition duration-150 group-hover/action:translate-y-0 group-hover/action:opacity-100 group-focus-within/action:translate-y-0 group-focus-within/action:opacity-100">
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

function discordSourceUrl(source?: CommandCenterSource | null): string | null {
  if (!source || !["discord", "discord_thread"].includes(source.kind)) return null;
  const ref = source.ref || {};
  for (const key of ["discord_url", "source_url", "discord_thread_url"]) {
    const value = ref[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function WorkItemCard({
  activeAction,
  item,
  multiSelectActionCommon,
  multiSelectActionUnion,
  onAction,
  onToggleSelected,
  selected,
  selectionActive,
}: {
  activeAction: ActiveAction | null;
  item: CommandCenterWorkItem;
  multiSelectActionCommon: Set<ActionKind>;
  multiSelectActionUnion: Set<ActionKind>;
  onAction: (kind: ActionKind, item: CommandCenterWorkItem) => void;
  onToggleSelected: (id: string) => void;
  selected: boolean;
  selectionActive: boolean;
}) {
  const rowBusy = Boolean(activeAction?.ids.includes(item.id));
  const actionBusy = (kind: ActionKind) => rowBusy && activeAction?.kind === kind;
  const singleActions = availableActionKinds(item);
  const actions = selected && selectionActive ? [...multiSelectActionUnion] : singleActions;
  const actionDisabled = (kind: ActionKind) => (Boolean(activeAction) && !actionBusy(kind)) || (selectionActive && (!selected || !multiSelectActionCommon.has(kind)));
  const disabledTitle = (kind: ActionKind) => {
    if (!selectionActive) return undefined;
    if (!selected) return "Clear selection before acting on this ticket";
    if (!multiSelectActionCommon.has(kind)) return "Not available for all selected tickets";
    return undefined;
  };
  const discordUrl = discordSourceUrl(item.source);
  const workerUrl = item.execution?.worker_url || null;
  const openWorker = () => {
    if (!workerUrl) return;
    window.open(workerUrl, "_blank", "noopener,noreferrer");
  };
  return (
    <article
      aria-label={workerUrl ? `Open worker board for ${item.title}` : undefined}
      aria-busy={rowBusy || undefined}
      className={cn(
        "command-center-card rounded-2xl border bg-[#08090a]/80 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.025)] transition",
        selected && "command-center-card-selected border-cyan-100/45 bg-cyan-100/[0.055]",
        workerUrl ? "cursor-pointer border-white/10 hover:border-cyan-100/35 hover:bg-cyan-100/[0.045] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-100/35" : "border-white/10 hover:border-white/20 hover:bg-white/[0.03]",
      )}
      onClick={workerUrl ? openWorker : undefined}
      onKeyDown={workerUrl ? (event) => {
        if (event.currentTarget !== event.target) return;
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openWorker();
      } : undefined}
      role={workerUrl ? "link" : undefined}
      tabIndex={workerUrl ? 0 : undefined}
    >
      <div className="flex items-start justify-between gap-3">
        <input
          aria-label={`Select ${item.title || item.id}`}
          checked={selected}
          className="mt-1 h-4 w-4 shrink-0 rounded border-white/20 bg-slate-950 text-cyan-300 accent-cyan-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-100/45"
          onChange={() => onToggleSelected(item.id)}
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
          type="checkbox"
        />
        <div className="min-w-0 flex-1 text-left">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <SourceBadge source={item.source} />
            <StatusPill value={item.status} />
            {item.project && <span className="text-xs uppercase tracking-[0.16em] text-slate-500">{item.project}</span>}
          </div>
          <h3 className="text-base font-semibold leading-snug text-white">{item.title}</h3>
        </div>
        {discordUrl ? (
          <div className="flex shrink-0 items-center gap-2">
            <a aria-label={`Open Discord source for ${item.title}`} className="inline-flex h-8 items-center gap-1.5 rounded-full border border-indigo-200/25 px-2.5 text-xs font-semibold text-indigo-100 transition hover:border-indigo-100/40 hover:bg-indigo-100/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-100/35" href={discordUrl} onClick={(event) => event.stopPropagation()} rel="noopener noreferrer" target="_blank">
              Discord <ExternalLink className="h-3 w-3" /><span className="sr-only">opens in a new tab</span>
            </a>
          </div>
        ) : null}
      </div>
      <div className="mt-3 block w-full text-left">
        <p className="text-sm leading-6 text-slate-300">{item.summary || item.body_preview || "No summary yet."}</p>
      </div>
      <div aria-busy={rowBusy} className="mt-4 flex flex-wrap items-center gap-2 border-t border-white/[0.08] pt-3">
        {item.execution?.task_url && item.execution.task_url !== item.execution.worker_url && (
          <a className="inline-flex h-9 items-center gap-1.5 rounded-full border border-white/10 px-3 text-xs font-semibold text-slate-200 transition hover:border-white/20 hover:bg-white/[0.07] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/20" href={item.execution.task_url} onClick={(event) => event.stopPropagation()} rel="noopener noreferrer" target="_blank">
            Ticket <ExternalLink className="h-3.5 w-3.5" /><span className="sr-only">opens in a new tab</span>
          </a>
        )}
        {rowBusy && activeAction && (
          <span className="inline-flex h-9 items-center gap-2 rounded-full border border-cyan-100/25 bg-cyan-100/10 px-3 text-xs font-semibold text-cyan-50" aria-live="polite">
            <Spinner /> {ACTION_PROGRESS_LABELS[activeAction.kind]}…
          </span>
        )}
        {actions.map((kind) => {
          const disabled = actionDisabled(kind);
          return (
            <ActionButton
              busy={actionBusy(kind)}
              disabled={disabled}
              key={kind}
              kind={kind}
              onClick={() => onAction(kind, item)}
              title={disabled ? disabledTitle(kind) : undefined}
            />
          );
        })}
        <div className="ml-auto min-w-fit pl-2 text-right text-[0.68rem] text-slate-500">
          Created {formatTime(item.created_at)}
        </div>
      </div>
    </article>
  );
}

function SourceCard({ source }: { source: CommandCenterSource }) {
  return (
    <article className="command-center-card rounded-2xl border border-white/10 bg-slate-950/45 p-4 text-left transition hover:border-cyan-100/35">
      <div className="flex flex-wrap items-center gap-2">
        <Badge className="border-white/10 bg-white/[0.055] text-slate-300">{source.label}</Badge>
        <StatusPill value={source.status} />
      </div>
      <div className="mt-3 text-sm font-semibold text-white">{source.title || source.id}</div>
      <div className="mt-2 text-xs text-slate-500">Updated {formatTime(source.updated_at || source.created_at)}</div>
      {typeof source.ref?.parse_error === "string" && (
        <p className="mt-3 line-clamp-3 text-xs leading-5 text-red-100/80">{source.ref.parse_error}</p>
      )}
    </article>
  );
}

function RunCard({ run }: { run: CommandCenterRun }) {
  const active = runIsActive(run);
  return (
    <article className="command-center-card rounded-2xl border border-white/10 bg-slate-950/45 p-4 text-left transition hover:border-cyan-100/35">
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill value={active ? "running" : run.outcome || run.status} />
        {run.board && <Badge className="border-white/10 bg-white/[0.055] text-slate-300">{run.board}</Badge>}
      </div>
      <div className="mt-3 text-sm font-semibold text-white">{run.task_title || run.task_id}</div>
      <div className="mt-2 text-xs text-slate-500">Started {formatTime(run.started_at)}{run.ended_at ? ` · Ended ${formatTime(run.ended_at)}` : ""}</div>
      {run.error && <p className="mt-3 line-clamp-3 text-xs leading-5 text-red-100/80">{run.error}</p>}
    </article>
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
  multiSelectActionCommon,
  multiSelectActionUnion,
  onAction,
  onToggleSelected,
  selectedIds,
  selectionActive,
}: {
  activeAction: ActiveAction | null;
  emptyLabel?: string;
  emptyMessage?: string;
  items: CommandCenterWorkItem[];
  multiSelectActionCommon: Set<ActionKind>;
  multiSelectActionUnion: Set<ActionKind>;
  onAction: (kind: ActionKind, item: CommandCenterWorkItem) => void;
  onToggleSelected: (id: string) => void;
  selectedIds: Set<string>;
  selectionActive: boolean;
}) {
  if (!items.length) return <EmptyState label={emptyLabel || "work items"} message={emptyMessage} />;
  return (
    <div className="grid gap-3">
      {[...items].sort(workItemViewSort).map((item) => (
        <WorkItemCard
          activeAction={activeAction}
          item={item}
          key={item.id}
          multiSelectActionCommon={multiSelectActionCommon}
          multiSelectActionUnion={multiSelectActionUnion}
          onAction={onAction}
          onToggleSelected={onToggleSelected}
          selected={selectedIds.has(item.id)}
          selectionActive={selectionActive}
        />
      ))}
    </div>
  );
}

function OverviewWorkList({
  activeAction,
  emptyMessage,
  items,
  multiSelectActionCommon,
  multiSelectActionUnion,
  onAction,
  onToggleSelected,
  selectedIds,
  selectionActive,
}: {
  activeAction: ActiveAction | null;
  emptyMessage?: string;
  items: CommandCenterWorkItem[];
  multiSelectActionCommon: Set<ActionKind>;
  multiSelectActionUnion: Set<ActionKind>;
  onAction: (kind: ActionKind, item: CommandCenterWorkItem) => void;
  onToggleSelected: (id: string) => void;
  selectedIds: Set<string>;
  selectionActive: boolean;
}) {
  if (!items.length) return <EmptyState label="work items" message={emptyMessage} />;

  const runningItems = items.filter(isRunningWorkItem).sort(workItemCreatedSort);
  const remainingItems = items.filter((item) => !isRunningWorkItem(item)).sort(workItemCreatedSort);
  const renderItem = (item: CommandCenterWorkItem) => (
    <WorkItemCard
      activeAction={activeAction}
      item={item}
      key={item.id}
      multiSelectActionCommon={multiSelectActionCommon}
      multiSelectActionUnion={multiSelectActionUnion}
      onAction={onAction}
      onToggleSelected={onToggleSelected}
      selected={selectedIds.has(item.id)}
      selectionActive={selectionActive}
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
  const selectedProject = useMemo(() => {
    const value = new URLSearchParams(location.search).get("project");
    return value && value.trim() ? value.trim().toLowerCase() : "hermes";
  }, [location.search]);
  const [snapshot, setSnapshot] = useState<CommandCenterSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeAction, setActiveAction] = useState<ActiveAction | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());

  const refresh = useCallback(async (options?: { delayBeforeApplyMs?: number; settleAfterApplyMs?: number }) => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.getCommandCenterSnapshot({ includeArchived: true, recentRunLimitPerBoard: 25, project: selectedProject });
      if (options?.delayBeforeApplyMs) {
        await waitForActionSettle(options.delayBeforeApplyMs);
      }
      setSnapshot(next);
      if (options?.settleAfterApplyMs) {
        await waitForActionSettle(options.settleAfterApplyMs);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [selectedProject]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async snapshot load mirrors existing dashboard data pages.
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const invokeRefresh = refresh;
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
  const workItemsById = useMemo(() => new Map((snapshot?.work_items ?? []).map((item) => [item.id, item])), [snapshot]);
  const selectedItems = useMemo(() => [...selectedIds].map((id) => workItemsById.get(id)).filter((item): item is CommandCenterWorkItem => Boolean(item)), [selectedIds, workItemsById]);
  const selectionActive = selectedItems.length > 1;
  const multiSelectActionUnion = useMemo(() => actionSet(selectedItems, "union"), [selectedItems]);
  const multiSelectActionCommon = useMemo(() => actionSet(selectedItems, "common"), [selectedItems]);
  const laneCounts = useMemo(() => ({
    overview: overviewItems.length,
    inbox: inboxItems.length + inboxSources.length,
    work: workItems.length,
    archive: archivedItems.length,
    workers: metric(snapshot, "active_runs"),
  }), [archivedItems.length, inboxItems.length, inboxSources.length, overviewItems.length, snapshot, workItems.length]);
  const projectSearch = useMemo(() => `?project=${encodeURIComponent(selectedProject)}`, [selectedProject]);
  const toggleSelected = useCallback((id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const clearSelection = useCallback(() => setSelectedIds(new Set()), []);
  const runActionForItem = useCallback(async (kind: ActionKind, item: CommandCenterWorkItem, rejectReason?: string) => {
    const proposalId = item.decision?.proposal_id;
    const board = item.execution?.board;
    if (kind === "archive") {
      if (board && board !== "default") await api.archiveKanbanBoard(board);
      else if (proposalId) await api.haltSelfImprovementProposal(proposalId);
    } else if (proposalId && kind === "approve") {
      await api.approveSelfImprovementProposal(proposalId);
    } else if (proposalId && kind === "reject") {
      if (!rejectReason) return;
      await api.rejectSelfImprovementProposal(proposalId, rejectReason);
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
  }, []);
  const handleAction = useCallback(async (kind: ActionKind, item: CommandCenterWorkItem) => {
    if (activeAction) return;
    const targetItems = selectionActive && selectedIds.has(item.id) ? selectedItems : [item];
    if (targetItems.length > 1 && !multiSelectActionCommon.has(kind)) return;
    const startedAt = Date.now();
    if (targetItems.some((targetItem) => !availableActionKinds(targetItem).includes(kind))) return;
    const rejectReason = kind === "reject" ? window.prompt("Reject reason for future prong feedback?", "Not worth doing right now.") : undefined;
    if (kind === "reject" && !rejectReason) return;
    setActiveAction({ ids: targetItems.map((targetItem) => targetItem.id), kind });
    setError(null);
    try {
      for (const targetItem of targetItems) {
        await runActionForItem(kind, targetItem, rejectReason || undefined);
      }
      await refresh({
        delayBeforeApplyMs: Math.max(0, ACTION_SETTLE_MS - (Date.now() - startedAt)),
        settleAfterApplyMs: ACTION_SETTLE_MS,
      });
      if (targetItems.length > 1) clearSelection();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setActiveAction(null);
    }
  }, [activeAction, clearSelection, multiSelectActionCommon, refresh, runActionForItem, selectedIds, selectedItems, selectionActive]);

  return (
    <div className="flex flex-col gap-5">
      <ProjectTabs
        currentProject={snapshot?.current_project || selectedProject}
        pathname={location.pathname}
        projects={snapshot?.projects ?? []}
      />
      <WorkStatePanel
        activeView={activeView}
        laneCounts={laneCounts}
        search={projectSearch}
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
        <section className="min-w-0">
          {selectedItems.length > 0 && (
            <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-slate-300">
              <span className="rounded-full border border-cyan-100/25 bg-cyan-100/10 px-3 py-1 font-semibold text-cyan-50">{selectedItems.length} selected</span>
              <button className="rounded-full border border-white/10 px-3 py-1 font-semibold text-slate-300 transition hover:border-white/20 hover:bg-white/[0.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/20" onClick={clearSelection} type="button">Clear</button>
            </div>
          )}
          {activeView === "overview" && (
            <OverviewWorkList activeAction={activeAction} emptyMessage="No recent decisions, worker boards, or active work yet." items={overviewItems} multiSelectActionCommon={multiSelectActionCommon} multiSelectActionUnion={multiSelectActionUnion} onAction={handleAction} onToggleSelected={toggleSelected} selectedIds={selectedIds} selectionActive={selectionActive} />
          )}
          {activeView === "inbox" && (
            <div className="grid gap-4">
              <WorkList activeAction={activeAction} emptyLabel="pending decisions" emptyMessage="Inbox is clear. Finished, blocked, and archiveable boards stay on Overview or Work." items={inboxItems} multiSelectActionCommon={multiSelectActionCommon} multiSelectActionUnion={multiSelectActionUnion} onAction={handleAction} onToggleSelected={toggleSelected} selectedIds={selectedIds} selectionActive={selectionActive} />
              {inboxSources.map((source) => <SourceCard key={source.id} source={source} />)}
            </div>
          )}
          {activeView === "work" && <WorkList activeAction={activeAction} emptyMessage="No active or recently shipped work is visible." items={workItems} multiSelectActionCommon={multiSelectActionCommon} multiSelectActionUnion={multiSelectActionUnion} onAction={handleAction} onToggleSelected={toggleSelected} selectedIds={selectedIds} selectionActive={selectionActive} />}
          {activeView === "archive" && <WorkList activeAction={activeAction} emptyLabel="archived items" emptyMessage="Archived worker boards and work items will appear here." items={archivedItems} multiSelectActionCommon={multiSelectActionCommon} multiSelectActionUnion={multiSelectActionUnion} onAction={handleAction} onToggleSelected={toggleSelected} selectedIds={selectedIds} selectionActive={selectionActive} />}
          {activeView === "recommendations" && <WorkList activeAction={activeAction} emptyLabel="recommendations" emptyMessage="No self-improvement recommendations are waiting." items={recommendations} multiSelectActionCommon={multiSelectActionCommon} multiSelectActionUnion={multiSelectActionUnion} onAction={handleAction} onToggleSelected={toggleSelected} selectedIds={selectedIds} selectionActive={selectionActive} />}
          {activeView === "runs" && (
            <div className="grid gap-3">
              {snapshot?.runs.length ? snapshot.runs.map((run) => (
                <RunCard key={`${run.board || "default"}:${run.id}`} run={run} />
              )) : <EmptyState label="worker runs" />}
            </div>
          )}
          {activeView === "sources" && (
            <div className="grid gap-3">
              {sources.length ? sources.map((source) => (
                <SourceCard key={source.id} source={source} />
              )) : <EmptyState label="sources" />}
            </div>
          )}
        </section>
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
