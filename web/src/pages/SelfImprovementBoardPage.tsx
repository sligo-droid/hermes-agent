import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, ExternalLink, FileText, RefreshCw, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";
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

type Selection =
  | { mode: "card"; card: SelfImprovementProposalCard }
  | { mode: "source" | "failure"; run: SelfImprovementProposalRun };

function formatTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function truncate(value: string, max = 180): string {
  return value.length > max ? `${value.slice(0, max).trimEnd()}...` : value;
}

function cardMeta(card: SelfImprovementProposalCard): string[] {
  return [
    card.run_id ? `run ${card.run_id}` : null,
    card.cron_job_id ? `cron ${card.cron_job_id}` : null,
    card.cron_output_path ? card.cron_output_path : null,
  ].filter((value): value is string => Boolean(value));
}

function priorityTone(priority: string): string {
  const normalized = priority.toLowerCase();
  if (normalized === "high" || normalized === "urgent") return "border-red-400/50 text-red-200";
  if (normalized === "medium") return "border-yellow-300/50 text-yellow-100";
  return "border-current/20 text-text-secondary";
}

function StatusPill({ value }: { value: string }) {
  return (
    <Badge className="w-fit border-current/20 bg-current/10 px-2 py-0.5 text-[0.68rem] uppercase tracking-[0.16em] text-text-secondary">
      {value || "unknown"}
    </Badge>
  );
}

function EmptyState({ error, onRetry }: { error: string | null; onRetry: () => void }) {
  return (
    <Card className="border-current/15 bg-card/70">
      <CardContent className="flex flex-col items-center gap-3 py-16 text-center">
        <Sparkles className="h-8 w-8 text-text-tertiary" />
        <div className="text-lg font-semibold text-midground">
          {error ? "Self-Improvement Board failed to load" : "No proposal cards yet"}
        </div>
        <p className="max-w-xl text-sm text-text-secondary">
          {error
            ? error
            : "Cron prongs have not emitted structured proposal cards for this profile yet. The Workers board remains available for downstream execution state."}
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

function ProposalCard({ card, selected, onSelect }: { card: SelfImprovementProposalCard; selected: boolean; onSelect: () => void }) {
  const meta = cardMeta(card);
  return (
    <button
      className={cn(
        "w-full rounded-lg border bg-card/70 p-4 text-left transition hover:border-current/35 hover:bg-card",
        selected ? "border-midground/70" : "border-current/15",
      )}
      onClick={onSelect}
      type="button"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="min-w-0 text-sm font-semibold leading-snug text-midground">
          {card.title}
        </h3>
        <div className="flex shrink-0 flex-wrap gap-1.5">
          <StatusPill value={card.status} />
          <Badge className={cn("w-fit border bg-transparent px-2 py-0.5 text-[0.68rem] uppercase tracking-[0.16em]", priorityTone(card.priority))}>
            {card.priority}
          </Badge>
        </div>
      </div>
      <p className="mt-3 text-sm leading-relaxed text-text-secondary">
        {truncate(card.summary || card.body || "No summary provided.")}
      </p>
      <div className="mt-3 flex flex-wrap gap-2 text-[0.7rem] uppercase tracking-[0.14em] text-text-tertiary">
        <span>{formatTime(card.created_at)}</span>
        {card.kanban_task_id && <span className="text-midground">worker {card.kanban_task_id}</span>}
        {meta.slice(0, 2).map((item) => (
          <span key={item} className="max-w-full truncate">
            {item}
          </span>
        ))}
      </div>
    </button>
  );
}

function DetailPanel({
  selection,
  actionBusy,
  actionMessage,
  onApprove,
  onReject,
  onLoadRun,
}: {
  selection: Selection | null;
  actionBusy: boolean;
  actionMessage: string | null;
  onApprove: (card: SelfImprovementProposalCard) => void;
  onReject: (card: SelfImprovementProposalCard) => void;
  onLoadRun: (runId: number, mode: DetailMode) => void;
}) {
  if (!selection) {
    return (
      <Card className="border-current/15 bg-card/60 lg:sticky lg:top-4">
        <CardContent className="flex min-h-80 flex-col items-center justify-center gap-3 text-center text-sm text-text-secondary">
          <FileText className="h-8 w-8 text-text-tertiary" />
          Select a proposal card, parse failure, or source run to inspect details.
        </CardContent>
      </Card>
    );
  }

  if (selection.mode === "card") {
    const { card } = selection;
    return (
      <Card className="border-current/15 bg-card/80 lg:sticky lg:top-4">
        <CardHeader>
          <div className="flex flex-wrap gap-2">
            <StatusPill value={card.status} />
            {card.severity && <StatusPill value={card.severity} />}
          </div>
          <CardTitle className="text-xl text-midground">{card.title}</CardTitle>
        </CardHeader>
        <CardContent className="flex max-h-[calc(100dvh-10rem)] flex-col gap-5 overflow-y-auto text-sm">
          <section>
            <h4 className="mb-2 text-xs uppercase tracking-[0.16em] text-text-tertiary">Summary</h4>
            <p className="leading-relaxed text-text-secondary">{card.summary || "No summary provided."}</p>
          </section>
          {card.rationale && (
            <section>
              <h4 className="mb-2 text-xs uppercase tracking-[0.16em] text-text-tertiary">Rationale</h4>
              <p className="leading-relaxed text-text-secondary">{card.rationale}</p>
            </section>
          )}
          {card.source_excerpts.length > 0 && (
            <section>
              <h4 className="mb-2 text-xs uppercase tracking-[0.16em] text-text-tertiary">Source Excerpts</h4>
              <div className="space-y-2">
                {card.source_excerpts.map((excerpt, idx) => (
                  <blockquote key={`${idx}:${excerpt.text.slice(0, 24)}`} className="rounded-md border border-current/10 bg-black/20 p-3 text-text-secondary">
                    {excerpt.label && <div className="mb-1 text-xs uppercase tracking-[0.14em] text-text-tertiary">{excerpt.label}</div>}
                    {excerpt.text}
                  </blockquote>
                ))}
              </div>
            </section>
          )}
          <section>
            <h4 className="mb-2 text-xs uppercase tracking-[0.16em] text-text-tertiary">Metadata</h4>
            <dl className="grid grid-cols-[8rem_1fr] gap-2 text-xs text-text-secondary">
              <dt>Project</dt><dd className="truncate text-midground">{card.project}</dd>
              <dt>Prong</dt><dd className="truncate text-midground">{card.prong}</dd>
              <dt>Proposal</dt><dd className="truncate font-mono-ui">{card.proposal_id}</dd>
              <dt>Run</dt><dd className="truncate font-mono-ui">{card.run_id || card.run_db_id}</dd>
              <dt>Cron job</dt><dd className="truncate font-mono-ui">{card.cron_job_id || "-"}</dd>
              <dt>Idempotency</dt><dd className="truncate font-mono-ui">{card.idempotency_key || "-"}</dd>
              <dt>Worker</dt><dd className="truncate font-mono-ui">{card.kanban_task_id || "-"}</dd>
            </dl>
          </section>
          {card.worker_url && (
            <a className="inline-flex w-fit items-center gap-1 text-sm text-midground underline underline-offset-4" href={card.worker_url}>
              Open worker task <ExternalLink className="h-3 w-3" />
            </a>
          )}
          {actionMessage && <div className="rounded-md border border-current/10 bg-black/20 p-3 text-xs text-text-secondary">{actionMessage}</div>}
          <div className="flex flex-wrap gap-2 border-t border-current/10 pt-4">
            <Button size="sm" onClick={() => onLoadRun(card.run_db_id, "source")}>View Source Run</Button>
            <Button disabled={actionBusy || card.status === "approved"} ghost size="sm" onClick={() => onApprove(card)}>
              {card.status === "approved" ? "Approved" : "Approve"}
            </Button>
            <Button disabled={actionBusy || card.status === "approved"} ghost size="sm" onClick={() => onReject(card)}>Reject</Button>
          </div>
        </CardContent>
      </Card>
    );
  }

  const { run } = selection;
  const source = selection.mode === "failure" ? run.parse_error : run.source_markdown;
  return (
    <Card className="border-current/15 bg-card/80 lg:sticky lg:top-4">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          {selection.mode === "failure" && <AlertTriangle className="h-4 w-4 text-warning" />}
          <StatusPill value={run.status} />
        </div>
        <CardTitle className="text-xl text-midground">
          {selection.mode === "failure" ? "Parse Failure" : "Source Cron Output"}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex max-h-[calc(100dvh-10rem)] flex-col gap-4 overflow-y-auto text-sm">
        <dl className="grid grid-cols-[7rem_1fr] gap-2 text-xs text-text-secondary">
          <dt>Project</dt><dd className="truncate text-midground">{run.project || "-"}</dd>
          <dt>Prong</dt><dd className="truncate text-midground">{run.prong || "-"}</dd>
          <dt>Run</dt><dd className="truncate font-mono-ui">{run.run_id || run.id}</dd>
          <dt>Cron job</dt><dd className="truncate font-mono-ui">{run.cron_job_id || "-"}</dd>
          <dt>Cards</dt><dd>{run.card_count}</dd>
          <dt>Updated</dt><dd>{formatTime(run.updated_at)}</dd>
        </dl>
        {run.source_url && (
          <a className="inline-flex items-center gap-1 text-sm text-midground underline underline-offset-4" href={run.source_url} rel="noreferrer" target="_blank">
            Open source <ExternalLink className="h-3 w-3" />
          </a>
        )}
        <pre className="max-h-[55dvh] overflow-auto whitespace-pre-wrap rounded-lg border border-current/10 bg-black/35 p-3 font-mono-ui text-xs leading-relaxed text-text-secondary">
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
  const [actionBusy, setActionBusy] = useState(false);
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
    void loadBoard();
  }, [loadBoard]);

  const cards = useMemo(
    () => data?.projects.flatMap((project) => project.prongs.flatMap((prong) => prong.cards)) ?? [],
    [data],
  );

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
    setActionBusy(true);
    setActionMessage(null);
    try {
      const response = await api.approveSelfImprovementProposal(card.proposal_id);
      replaceCard(response.card);
      setActionMessage(`Approved into Kanban task ${response.card.kanban_task_id || "unknown"}.`);
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  }, [replaceCard]);

  const rejectCard = useCallback(async (card: SelfImprovementProposalCard) => {
    const reason = window.prompt("Reason for rejecting this proposal?")?.trim();
    if (!reason) return;
    setActionBusy(true);
    setActionMessage(null);
    try {
      const response = await api.rejectSelfImprovementProposal(card.proposal_id, reason);
      setSelection(null);
      setData((current) => {
        if (!current) return current;
        return {
          projects: current.projects.map((project) => ({
            ...project,
            prongs: project.prongs.map((prong) => ({
              ...prong,
              cards: prong.cards.filter((item) => item.proposal_id !== response.card.proposal_id),
            })),
          })).filter((project) => project.prongs.some((prong) => prong.cards.length > 0)),
        };
      });
      setActionMessage("Proposal rejected and archived from the default board view.");
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  }, []);

  return (
    <div className="flex flex-col gap-5">
      <div className="rounded-xl border border-cyan-200/20 bg-[linear-gradient(135deg,rgba(8,47,73,0.7),rgba(2,6,23,0.82))] p-4">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-cyan-100/80">Sligo operator workflow</p>
            <p className="mt-1 max-w-3xl text-sm text-cyan-50/75">
              Review upstream proposal cards here, then send accepted work to the real Hermes Workers board for downstream execution.
            </p>
          </div>
          <Link className="inline-flex w-fit items-center gap-2 rounded border border-cyan-100/25 px-3 py-1.5 text-sm text-cyan-50 hover:bg-cyan-100/10" to="/sligo">
            Operator Home <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </div>

      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-text-tertiary">Upstream proposal review</p>
          <h1 className="mt-1 text-2xl font-semibold text-midground">Self-Improvement Board</h1>
          <p className="mt-2 max-w-3xl text-sm text-text-secondary">
            Read-only proposal cards from cron prongs. Workers remains the separate downstream execution board.
          </p>
        </div>
        <Button className="w-fit" disabled={loading} ghost size="sm" onClick={() => void loadBoard()}>
          {loading ? <Spinner /> : <RefreshCw className="h-4 w-4" />} Refresh
        </Button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-24"><Spinner className="text-2xl text-primary" /></div>
      ) : !data || error || (cards.length === 0 && failures.length === 0) ? (
        <EmptyState error={error} onRetry={() => void loadBoard()} />
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(22rem,28rem)]">
          <div className="flex min-w-0 flex-col gap-5">
            {failures.length > 0 && (
              <Card className="border-warning/40 bg-card/70">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-base text-midground">
                    <AlertTriangle className="h-4 w-4 text-warning" /> Parse Failures
                  </CardTitle>
                </CardHeader>
                <CardContent className="grid gap-2">
                  {failures.map((failure) => (
                    <button
                      className="rounded-md border border-current/10 bg-black/15 p-3 text-left text-sm text-text-secondary hover:border-warning/60"
                      key={failure.id}
                      onClick={() => void loadRun(failure.id, "failure")}
                      type="button"
                    >
                      <div className="font-medium text-midground">{failure.cron_job_name || failure.cron_job_id || failure.source_key}</div>
                      <div className="mt-1 line-clamp-2">{failure.parse_error || "Malformed proposal output"}</div>
                    </button>
                  ))}
                </CardContent>
              </Card>
            )}

            {data.projects.map((project) => (
              <section className="flex flex-col gap-3" key={project.project}>
                <div className="flex items-center justify-between gap-3 border-b border-current/10 pb-2">
                  <h2 className="text-lg font-semibold text-midground">{project.project}</h2>
                  <span className="text-xs uppercase tracking-[0.16em] text-text-tertiary">
                    {project.prongs.reduce((count, prong) => count + prong.cards.length, 0)} proposals
                  </span>
                </div>
                <div className="grid gap-4 xl:grid-cols-2">
                  {project.prongs.map((prong) => (
                    <Card className="border-current/15 bg-card/45" key={`${project.project}:${prong.prong}`}>
                      <CardHeader className="pb-3">
                        <CardTitle className="text-sm uppercase tracking-[0.16em] text-text-secondary">
                          {prong.prong}
                        </CardTitle>
                      </CardHeader>
                      <CardContent className="flex flex-col gap-3">
                        {prong.cards.map((card) => (
                          <ProposalCard
                            card={card}
                            key={card.proposal_id}
                            onSelect={() => setSelection({ mode: "card", card })}
                            selected={selection?.mode === "card" && selection.card.proposal_id === card.proposal_id}
                          />
                        ))}
                      </CardContent>
                    </Card>
                  ))}
                </div>
              </section>
            ))}
          </div>

          <div className="min-w-0">
            {detailLoading ? (
              <Card className="border-current/15 bg-card/60"><CardContent className="flex min-h-80 items-center justify-center"><Spinner /></CardContent></Card>
            ) : (
              <DetailPanel
                actionBusy={actionBusy}
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
