export type WorkerConsoleSnapshot = {
  board: string;
  backend: "codex" | "opencode" | string;
  task: {
    id: string;
    title: string;
    status: string;
    assignee?: string | null;
    worker_pid?: number | null;
    current_run_id?: number | null;
  };
  workspace: {
    path: string;
    kind: string;
    available: boolean;
  };
  current_run?: {
    id?: number | null;
    status?: string | null;
    outcome?: string | null;
    worker_pid?: number | null;
    started_at?: number | null;
    last_heartbeat_at?: number | null;
  } | null;
  events: unknown[];
  worker_log_path: string;
  worker_log_tail: string;
  codex_state: unknown;
  operator_console_text?: string;
  updated_at: number;
};

export function normalizeTerminalText(text: string): string {
  return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\n/g, "\r\n");
}

function terminalSafeLine(value: unknown): string {
  return String(value ?? "-").replace(/\r/g, " ").replace(/\n/g, " ");
}

export function snapshotConsoleText(snapshot: WorkerConsoleSnapshot, reason: string): string {
  const run = snapshot.current_run;
  const lines = [
    "Hermes worker console (read-only)",
    `ticket: ${terminalSafeLine(snapshot.task.id)}`,
    `title: ${terminalSafeLine(snapshot.task.title)}`,
    `status: ${terminalSafeLine(snapshot.task.status)}`,
    `backend: ${terminalSafeLine(snapshot.backend || "unknown")}`,
    `run: ${terminalSafeLine(run?.id || snapshot.task.current_run_id || "-")}`,
    `pid: ${terminalSafeLine(run?.worker_pid || snapshot.task.worker_pid || "-")}`,
    `workspace: ${terminalSafeLine(snapshot.workspace.path || "-")}`,
    `worker log: ${terminalSafeLine(snapshot.worker_log_path || "-")}`,
    `stream: ${reason}`,
    "",
  ];
  const activity = snapshot.operator_console_text?.trimEnd();
  if (activity) {
    lines.push(activity);
  } else if (snapshot.worker_log_tail?.trimEnd()) {
    lines.push("[worker log]");
    lines.push(snapshot.worker_log_tail.trimEnd());
  } else {
    lines.push("[backend activity] waiting for coding worker backend events");
  }
  return normalizeTerminalText(`${lines.join("\n")}\n`);
}
