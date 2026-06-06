import assert from "node:assert/strict";
import { Script } from "node:vm";
import ts from "typescript";
import { readFileSync } from "node:fs";

const sourcePath = new URL("../src/pages/workerConsoleTerminal.ts", import.meta.url).pathname;
const result = ts.transpileModule(readFileSync(sourcePath, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});

const module = { exports: {} };
new Script(result.outputText).runInNewContext({ module, exports: module.exports });
const { normalizeTerminalText, snapshotConsoleText } = module.exports;

function assertNoBareLf(text) {
  assert.equal(/(^|[^\r])\n/.test(text), false, JSON.stringify(text));
}

assert.equal(normalizeTerminalText("one\ntwo\rthree\r\nfour"), "one\r\ntwo\r\nthree\r\nfour");

const baseSnapshot = {
  board: "board",
  backend: "opencode",
  task: { id: "t-1", title: "Ticket", status: "running" },
  workspace: { path: "/repo", kind: "worktree", available: true },
  current_run: { id: 12, worker_pid: 345 },
  events: [],
  worker_log_path: "/tmp/worker.log",
  worker_log_tail: "177 running_roles_by_board\n178 ready_roles",
  codex_state: {},
  updated_at: 1,
};

const activityText = snapshotConsoleText(
  {
    ...baseSnapshot,
    operator_console_text: "[command completed]\noutput:\n177 running_roles_by_board\n178 ready_roles",
  },
  "REST snapshot while websocket connects",
);
assert.match(activityText, /\[command completed\]\r\noutput:\r\n177 running_roles_by_board\r\n178 ready_roles\r\n/);
assertNoBareLf(activityText);

const logTailText = snapshotConsoleText(baseSnapshot, "websocket closed; showing REST snapshot");
assert.match(logTailText, /\[worker log\]\r\n177 running_roles_by_board\r\n178 ready_roles\r\n/);
assertNoBareLf(logTailText);

console.log("worker console terminal text normalization ok");
