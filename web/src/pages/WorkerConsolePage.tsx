import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { Button } from "@nous-research/ui/ui/components/button";
import { Typography } from "@nous-research/ui/ui/components/typography/index";
import { HERMES_BASE_PATH, buildWsAuthParam, fetchJSON } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ArrowLeft, ExternalLink, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { snapshotConsoleText, type WorkerConsoleSnapshot } from "./workerConsoleTerminal";

function workerPath(sessionId: string, taskId?: string, suffix = ""): string {
  const base = `/workers/${encodeURIComponent(sessionId)}`;
  if (!taskId) return base;
  return `${base}/tickets/${encodeURIComponent(taskId)}${suffix}`;
}

function dashboardHref(path: string): string {
  return `${HERMES_BASE_PATH}${path}`;
}

function buildConsoleWsUrl(
  sessionId: string,
  taskId: string,
  authParam: [string, string],
): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const qs = new URLSearchParams({ [authParam[0]]: authParam[1] });
  return (
    `${proto}//${window.location.host}${HERMES_BASE_PATH}` +
    `/api/workers/${encodeURIComponent(sessionId)}` +
    `/tickets/${encodeURIComponent(taskId)}/console/pty?${qs.toString()}`
  );
}

function formatTimestamp(value?: number | null): string {
  if (!value) return "-";
  return new Date(value * 1000).toLocaleString();
}

function writeSnapshotFallback(
  term: Terminal,
  snapshot: WorkerConsoleSnapshot,
  reason: string,
) {
  term.reset();
  term.write(snapshotConsoleText(snapshot, reason));
}

export default function WorkerConsolePage() {
  const { sessionId = "", taskId = "" } = useParams();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const snapshotRef = useRef<WorkerConsoleSnapshot | null>(null);
  const streamReceivedRef = useRef(false);
  const fallbackSnapshotRef = useRef("");
  const [snapshot, setSnapshot] = useState<WorkerConsoleSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [terminalStatus, setTerminalStatus] = useState("connecting");

  const boardUrl = useMemo(() => dashboardHref(workerPath(sessionId)), [sessionId]);
  const ticketUrl = useMemo(
    () => dashboardHref(workerPath(sessionId, taskId)),
    [sessionId, taskId],
  );
  const sanitizedTerminalUrl = useMemo(
    () => dashboardHref(workerPath(sessionId, taskId, "/terminal")),
    [sessionId, taskId],
  );

  const loadSnapshot = useCallback(async () => {
    if (!sessionId || !taskId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchJSON<WorkerConsoleSnapshot>(
        `/api/workers/${encodeURIComponent(sessionId)}` +
          `/tickets/${encodeURIComponent(taskId)}/console`,
      );
      setSnapshot(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [sessionId, taskId]);

  useEffect(() => {
    if (!sessionId || !taskId) return;
    let cancelled = false;
    void fetchJSON<WorkerConsoleSnapshot>(
      `/api/workers/${encodeURIComponent(sessionId)}` +
        `/tickets/${encodeURIComponent(taskId)}/console`,
    )
      .then((data) => {
        if (cancelled) return;
        setSnapshot(data);
        setError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, taskId]);

  useEffect(() => {
    snapshotRef.current = snapshot;
    const term = termRef.current;
    if (!snapshot || !term || streamReceivedRef.current) return;
    if (terminalStatus === "connected") return;
    const marker = `${snapshot.updated_at}:${snapshot.operator_console_text?.length || 0}:${snapshot.worker_log_tail.length}:${terminalStatus}`;
    if (fallbackSnapshotRef.current === marker) return;
    fallbackSnapshotRef.current = marker;
    writeSnapshotFallback(
      term,
      snapshot,
      terminalStatus === "connecting"
        ? "REST snapshot while websocket connects"
        : `websocket ${terminalStatus}; showing REST snapshot`,
    );
  }, [snapshot, terminalStatus]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !sessionId || !taskId) return;

    const term = new Terminal({
      cursorBlink: false,
      disableStdin: true,
      fontFamily:
        "'JetBrains Mono', 'Cascadia Mono', 'Fira Code', Menlo, Consolas, monospace",
      fontSize: 13,
      lineHeight: 1.15,
      scrollback: 5000,
      theme: {
        background: "#050816",
        foreground: "#f8fafc",
        cursor: "#f8fafc",
        selectionBackground: "#93c5fd44",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon());
    term.open(host);
    termRef.current = term;
    streamReceivedRef.current = false;
    fallbackSnapshotRef.current = "";
    if (snapshotRef.current) {
      fallbackSnapshotRef.current = `${snapshotRef.current.updated_at}:${snapshotRef.current.operator_console_text?.length || 0}:${snapshotRef.current.worker_log_tail.length}:connecting`;
      writeSnapshotFallback(term, snapshotRef.current, "REST snapshot while websocket connects");
    }

    const fitNow = () => {
      if (!host.isConnected || host.clientWidth <= 0 || host.clientHeight <= 0) return;
      try {
        fit.fit();
      } catch {
        return;
      }
    };

    const resizeObserver = new ResizeObserver(fitNow);
    resizeObserver.observe(host);
    requestAnimationFrame(fitNow);

    let unmounting = false;
    let ws: WebSocket | null = null;
    void (async () => {
      try {
        const authParam = await buildWsAuthParam();
        if (unmounting) return;
        ws = new WebSocket(buildConsoleWsUrl(sessionId, taskId, authParam));
        ws.binaryType = "arraybuffer";
        wsRef.current = ws;

        ws.onopen = () => {
          setTerminalStatus("connected");
          fitNow();
        };
        ws.onmessage = (event) => {
          streamReceivedRef.current = true;
          if (typeof event.data === "string") {
            term.write(event.data);
          } else if (event.data instanceof ArrayBuffer) {
            term.write(new Uint8Array(event.data));
          }
        };
        ws.onerror = () => {
          setTerminalStatus("error");
          if (!streamReceivedRef.current && snapshotRef.current) {
            writeSnapshotFallback(
              term,
              snapshotRef.current,
              "websocket error; showing REST snapshot",
            );
          }
        };
        ws.onclose = () => {
          setTerminalStatus("closed");
          if (!streamReceivedRef.current && snapshotRef.current) {
            writeSnapshotFallback(
              term,
              snapshotRef.current,
              "websocket closed; showing REST snapshot",
            );
          }
        };
      } catch (err) {
        if (!unmounting) {
          setTerminalStatus(err instanceof Error ? err.message : "auth error");
          if (!streamReceivedRef.current && snapshotRef.current) {
            writeSnapshotFallback(
              term,
              snapshotRef.current,
              "websocket auth failed; showing REST snapshot",
            );
          }
        }
      }
    })();

    return () => {
      unmounting = true;
      resizeObserver.disconnect();
      ws?.close();
      term.dispose();
      if (wsRef.current === ws) wsRef.current = null;
      if (termRef.current === term) termRef.current = null;
    };
  }, [sessionId, taskId]);

  const logText = snapshot?.worker_log_tail?.trim()
    ? snapshot.worker_log_tail
    : "(no worker log captured yet)";
  const codexState = snapshot?.codex_state && typeof snapshot.codex_state === "object"
    ? (snapshot.codex_state as Record<string, unknown>)
    : {};
  const toolTraceCount = Array.isArray(codexState.tool_trace) ? codexState.tool_trace.length : 0;
  const backendEventCount = Array.isArray(codexState.events) ? codexState.events.length : 0;
  const truncatedEvents = typeof codexState.truncated_events === "number" ? codexState.truncated_events : 0;
  const hasResult = Boolean(codexState.result);
  const run = snapshot?.current_run;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 text-text-primary">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <a
            className="inline-flex items-center gap-2 text-sm text-text-secondary hover:text-midground"
            href={boardUrl}
          >
            <ArrowLeft className="h-4 w-4" />
            Back to board
          </a>
          <Typography className="mt-2 truncate text-2xl font-bold text-midground">
            {snapshot?.task.title || "Worker Console"}
          </Typography>
          <div className="mt-2 flex flex-wrap gap-2 text-xs text-text-secondary">
            <span>ticket: {taskId}</span>
            <span>backend: {snapshot?.backend || "unknown"}</span>
            <span>status: {snapshot?.task.status || "unknown"}</span>
            <span>stream: {terminalStatus}</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button ghost size="sm" onClick={() => void loadSnapshot()} disabled={loading}>
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
            Refresh
          </Button>
          <a
            className="inline-flex items-center rounded border border-current/20 px-3 py-1.5 text-sm text-text-secondary hover:text-midground"
            href={ticketUrl}
          >
            Ticket
          </a>
          <a
            className="inline-flex items-center gap-2 rounded border border-current/20 px-3 py-1.5 text-sm text-text-secondary hover:text-midground"
            href={sanitizedTerminalUrl}
          >
            Sanitized Feed
            <ExternalLink className="h-4 w-4" />
          </a>
        </div>
      </div>

      {error && (
        <div className="rounded border border-red-400/40 bg-red-950/40 p-3 text-sm text-red-100">
          {error}
        </div>
      )}

      <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[minmax(0,1fr)_390px]">
        <section className="flex min-h-[520px] min-w-0 flex-col overflow-hidden rounded-lg border border-current/20 bg-black/70">
          <div className="flex items-center justify-between border-b border-current/15 px-3 py-2 text-xs text-text-secondary">
            <span>Codex/OpenCode backend activity (read-only)</span>
            <span>{snapshot?.workspace.available ? snapshot.workspace.path : "workspace unavailable"}</span>
          </div>
          <div ref={hostRef} className="min-h-0 flex-1 p-2" />
        </section>

        <aside className="flex min-h-0 flex-col gap-3 overflow-hidden">
          <section className="rounded-lg border border-current/20 bg-background-base/70 p-3">
            <Typography className="text-sm font-bold text-midground">Run</Typography>
            <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs text-text-secondary">
              <dt>workspace</dt>
              <dd className="truncate">{snapshot?.workspace.path || "-"}</dd>
              <dt>available</dt>
              <dd>{snapshot?.workspace.available ? "yes" : "no"}</dd>
              <dt>run</dt>
              <dd>{run?.id || snapshot?.task.current_run_id || "-"}</dd>
              <dt>pid</dt>
              <dd>{run?.worker_pid || snapshot?.task.worker_pid || "-"}</dd>
              <dt>started</dt>
              <dd>{formatTimestamp(run?.started_at)}</dd>
              <dt>heartbeat</dt>
              <dd>{formatTimestamp(run?.last_heartbeat_at)}</dd>
              <dt>log</dt>
              <dd className="truncate">{snapshot?.worker_log_path || "-"}</dd>
            </dl>
          </section>

          <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-current/20 bg-background-base/70">
            <div className="border-b border-current/15 px-3 py-2 text-sm font-bold text-midground">
              Worker log tail
            </div>
            <pre className="min-h-0 flex-1 overflow-auto p-3 text-xs leading-relaxed text-text-secondary whitespace-pre-wrap">
              {logText}
            </pre>
          </section>

          <section className="rounded-lg border border-current/20 bg-background-base/70 p-3">
            <div className="border-b border-current/15 px-3 py-2 text-sm font-bold text-midground">
              Backend activity metadata
            </div>
            <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs text-text-secondary">
              <dt>retained events</dt>
              <dd>{backendEventCount}</dd>
              <dt>truncated events</dt>
              <dd>{truncatedEvents}</dd>
              <dt>tool trace</dt>
              <dd>{toolTraceCount}</dd>
              <dt>result</dt>
              <dd>{hasResult ? "captured" : "pending"}</dd>
            </dl>
          </section>
        </aside>
      </div>
    </div>
  );
}
