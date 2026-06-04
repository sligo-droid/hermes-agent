/** Sligo Self-Improvement — Dashboard Plugin. */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React, fetchJSON } = SDK;
  const h = React.createElement;
  const { useCallback, useEffect, useMemo, useState } = SDK.hooks;
  const { Button, Card, CardContent, Badge, Select, SelectOption, Input, Label } = SDK.components;
  const { timeAgo } = SDK.utils;

  const API = "/api/plugins/sligo";
  const STATUSES = ["proposed", "enqueued", "running", "done", "blocked", "failed", "rejected", "archived"];
  const DEFAULT_STATUS = "proposed";

  function apiGet(path) {
    return fetchJSON(API + path);
  }

  function apiPost(path, body) {
    return fetchJSON(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  function parseApiError(err) {
    const raw = err && err.message ? String(err.message) : String(err || "Unknown error");
    const m = raw.match(/^\d{3}:\s*(.*)$/s);
    const body = m ? m[1] : raw;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed.detail === "string") return parsed.detail;
      if (parsed.detail && typeof parsed.detail.message === "string") return parsed.detail.message;
    } catch (_e) {}
    return body;
  }

  function option(value, label) {
    return h(SelectOption, { key: value, value }, label || value);
  }

  function fmtDate(value) {
    if (!value) return "—";
    try { return timeAgo(Number(value) * 1000); } catch (_e) { return String(value); }
  }

  function statusTone(status) {
    if (status === "proposed") return "si-badge si-badge-proposed";
    if (status === "rejected" || status === "archived") return "si-badge si-badge-rejected";
    if (status === "blocked" || status === "failed") return "si-badge si-badge-risk";
    if (status === "done") return "si-badge si-badge-done";
    return "si-badge si-badge-active";
  }

  function normalizeProjects(projects) {
    return (projects || []).map((p) => ({ ...p, prongs: p.prongs || [] }));
  }

  function groupByProng(cards, prongs) {
    const columns = new Map();
    for (const prong of prongs) columns.set(prong.slug, { prong, cards: [] });
    for (const card of cards) {
      const key = card.prong_slug || "other";
      if (!columns.has(key)) columns.set(key, { prong: { slug: key, label: key }, cards: [] });
      columns.get(key).cards.push(card);
    }
    return Array.from(columns.values()).filter((col) => col.cards.length || col.prong.slug !== "other");
  }

  function EvidenceList({ items }) {
    if (!items || !items.length) return h("p", { className: "si-muted" }, "No evidence attached.");
    return h("ul", { className: "si-list" }, items.map((item, index) => h("li", { key: index }, item)));
  }

  function CardTile({ card, onOpen, onApprove, onReject, busyId }) {
    const busy = busyId === card.id;
    return h(Card, { className: "si-card" },
      h(CardContent, { className: "si-card-content" },
        h("div", { className: "si-card-top" },
          h(Badge, { className: statusTone(card.status) }, card.status),
          h("span", { className: "si-effort" }, card.estimated_effort || "effort n/a"),
        ),
        h("button", { className: "si-card-title", onClick: () => onOpen(card) }, card.title),
        h("p", { className: "si-summary" }, card.summary || card.recommended_action || "No summary."),
        h("div", { className: "si-meta-row" },
          h("span", null, `p${card.priority ?? 0}`),
          h("span", null, card.confidence == null ? "confidence n/a" : `${Math.round(Number(card.confidence) * 100)}% confidence`),
          h("span", null, fmtDate(card.created_at)),
        ),
        h("div", { className: "si-actions" },
          h(Button, { size: "sm", variant: "secondary", onClick: () => onOpen(card) }, "Details"),
          card.status === "proposed" && h(Button, { size: "sm", onClick: () => onApprove(card), disabled: busy }, busy ? "Approving…" : "Approve"),
          card.status === "proposed" && h(Button, { size: "sm", variant: "ghost", onClick: () => onReject(card), disabled: busy }, "Reject"),
          card.worker_public_url && h("a", { className: "si-worker-link", href: card.worker_public_url }, "Worker →"),
        )
      )
    );
  }

  function DetailDrawer({ card, onClose, onApprove, onReject, busyId }) {
    if (!card) return null;
    const busy = busyId === card.id;
    return h("div", { className: "si-drawer-backdrop", onClick: onClose },
      h("aside", { className: "si-drawer", onClick: (e) => e.stopPropagation() },
        h("div", { className: "si-drawer-header" },
          h("div", null,
            h("p", { className: "si-kicker" }, `${card.project_slug} / ${card.prong_slug}`),
            h("h2", null, card.title),
          ),
          h(Button, { variant: "ghost", onClick: onClose }, "Close"),
        ),
        h("div", { className: "si-drawer-status" },
          h(Badge, { className: statusTone(card.status) }, card.status),
          h("span", null, `Run ${card.run_id}`),
        ),
        h("section", null,
          h("h3", null, "Summary"),
          h("p", null, card.summary || "No summary."),
        ),
        h("section", null,
          h("h3", null, "Rationale"),
          h("p", null, card.body || card.recommended_action || "No rationale."),
        ),
        h("section", null,
          h("h3", null, "Evidence"),
          h(EvidenceList, { items: card.evidence }),
        ),
        h("section", null,
          h("h3", null, "Worker prompt"),
          h("pre", { className: "si-pre" }, card.worker_prompt || card.recommended_action || card.title),
        ),
        h("section", null,
          h("h3", null, "Acceptance criteria"),
          h(EvidenceList, { items: card.acceptance_criteria }),
        ),
        card.risk_notes && h("section", null,
          h("h3", null, "Risk notes"),
          h("p", null, card.risk_notes),
        ),
        h("section", null,
          h("h3", null, "Source output"),
          card.run ? h("dl", { className: "si-definition" },
            h("dt", null, "Cron job"), h("dd", null, card.run.cron_job_name || card.run.cron_job_id || "manual"),
            h("dt", null, "Path"), h("dd", null, card.run.cron_output_path || "—"),
            h("dt", null, "SHA-256"), h("dd", null, card.run.cron_output_sha256 || "—"),
            h("dt", null, "Parser"), h("dd", null, `${card.run.parse_status} · ${card.run.parser_version}`),
          ) : h("p", { className: "si-muted" }, "Open details loaded from the proposal API to see source metadata."),
          card.run && card.run.raw_summary && h("details", { className: "si-source-details" },
            h("summary", null, "Show sanitized source markdown"),
            h("pre", { className: "si-pre" }, card.run.raw_summary),
          ),
        ),
        h("section", null,
          h("h3", null, "Execution"),
          h("dl", { className: "si-definition" },
            h("dt", null, "Board"), h("dd", null, card.kanban_board || "—"),
            h("dt", null, "Task"), h("dd", null, card.kanban_task_id || "not enqueued"),
            h("dt", null, "Workspace"), h("dd", null, `${card.workspace_kind || "scratch"}${card.workspace_path ? ` · ${card.workspace_path}` : ""}`),
          ),
          card.worker_public_url && h("a", { className: "si-primary-link", href: card.worker_public_url }, "Open linked worker board"),
        ),
        h("div", { className: "si-drawer-actions" },
          card.status === "proposed" && h(Button, { onClick: () => onApprove(card), disabled: busy }, busy ? "Approving…" : "Approve and enqueue"),
          card.status === "proposed" && h(Button, { variant: "secondary", onClick: () => onReject(card), disabled: busy }, "Reject"),
        )
      )
    );
  }

  function RunList({ runs, selectedRun, onSelectRun }) {
    if (!runs.length) return h("p", { className: "si-muted" }, "No proposal runs ingested yet.");
    return h("div", { className: "si-runs" }, runs.slice(0, 8).map((run) =>
      h("button", {
        key: run.id,
        className: selectedRun === run.id ? "si-run si-run-active" : "si-run",
        onClick: () => onSelectRun(selectedRun === run.id ? "" : run.id),
      },
        h("strong", null, run.cron_job_name || run.prong_name || run.id),
        h("span", null, `${run.parse_status} · ${fmtDate(run.created_at)}`),
        run.parse_status === "failed" && h("span", { className: "si-run-error" }, run.parse_error || "Parse failed"),
        run.parse_status === "failed" && run.raw_summary && h("details", { className: "si-run-source", onClick: (e) => e.stopPropagation() },
          h("summary", null, "Source output"),
          h("pre", { className: "si-pre" }, run.raw_summary),
        ),
      )
    ));
  }

  function SelfImprovementApp() {
    const [projects, setProjects] = useState([]);
    const [selectedProject, setSelectedProject] = useState("pid");
    const [status, setStatus] = useState(DEFAULT_STATUS);
    const [includeArchived, setIncludeArchived] = useState(false);
    const [cards, setCards] = useState([]);
    const [runs, setRuns] = useState([]);
    const [selectedRun, setSelectedRun] = useState("");
    const [active, setActive] = useState(null);
    const [busyId, setBusyId] = useState("");
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const project = useMemo(() => projects.find((p) => p.slug === selectedProject) || projects[0], [projects, selectedProject]);
    const prongs = project ? project.prongs || [] : [];
    const visibleCards = useMemo(() => {
      if (!selectedRun) return cards;
      return cards.filter((card) => card.run_id === selectedRun);
    }, [cards, selectedRun]);
    const columns = useMemo(() => groupByProng(visibleCards, prongs), [visibleCards, prongs]);

    const loadProjects = useCallback(async () => {
      const data = await apiGet("/projects");
      const loaded = normalizeProjects(data.projects);
      setProjects(loaded);
      if (loaded.length && !loaded.some((p) => p.slug === selectedProject)) {
        setSelectedProject(loaded[0].slug);
      }
    }, [selectedProject]);

    const loadBoard = useCallback(async () => {
      if (!selectedProject) return;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({ project: selectedProject, limit: "200" });
        if (status) params.set("status", status);
        if (includeArchived) params.set("include_archived", "true");
        const [proposalData, runData] = await Promise.all([
          apiGet(`/proposals?${params.toString()}`),
          apiGet(`/runs?project=${encodeURIComponent(selectedProject)}&limit=30`),
        ]);
        setCards(proposalData.proposals || []);
        setRuns(runData.runs || []);
      } catch (err) {
        setError(parseApiError(err));
      } finally {
        setLoading(false);
      }
    }, [selectedProject, status, includeArchived]);

    useEffect(() => { void loadProjects(); }, [loadProjects]);
    useEffect(() => { void loadBoard(); }, [loadBoard]);

    const openCard = useCallback(async (card) => {
      setActive(card);
      setError(null);
      try {
        const detail = await apiGet(`/proposals/${encodeURIComponent(card.id)}`);
        setActive(detail);
        setCards((prev) => prev.map((item) => item.id === card.id ? { ...item, ...detail } : item));
      } catch (err) {
        setError(parseApiError(err));
      }
    }, []);

    const act = useCallback(async (kind, card) => {
      const reason = kind === "reject" ? window.prompt("Reason for rejection?", "") : "Approved from Self-Improvement Board";
      if (kind === "reject" && reason === null) return;
      setBusyId(card.id);
      setError(null);
      try {
        const updated = await apiPost(`/proposals/${encodeURIComponent(card.id)}/${kind}`, {
          operator: "dashboard",
          reason: reason || undefined,
        });
        setCards((prev) => prev.map((item) => item.id === card.id ? updated : item).filter((item) => status || item.status !== "rejected"));
        setActive((current) => current && current.id === card.id ? updated : current);
        await loadBoard();
      } catch (err) {
        setError(parseApiError(err));
      } finally {
        setBusyId("");
      }
    }, [loadBoard, status]);

    return h("div", { className: "sligo-self-improvement" },
      h("header", { className: "si-hero" },
        h("div", null,
          h("p", { className: "si-kicker" }, "Sligo operator surface"),
          h("h1", null, "Self-Improvement Board"),
          h("p", { className: "si-hero-copy" }, "Review cron-generated proposal cards, approve useful work into Kanban execution, and retain rejection feedback for future prongs."),
        ),
        h("div", { className: "si-hero-stats" },
          h("div", null, h("strong", null, String(cards.length)), h("span", null, "cards")),
          h("div", null, h("strong", null, String(runs.length)), h("span", null, "runs")),
          h("div", null, h("strong", null, project ? project.prongs.length : 0), h("span", null, "prongs")),
        ),
      ),
      h("section", { className: "si-controls" },
        h("div", null,
          h(Label, null, "Project"),
          h(Select, { value: selectedProject, onValueChange: setSelectedProject },
            projects.map((p) => option(p.slug, p.name || p.slug)),
          ),
        ),
        h("div", null,
          h(Label, null, "Status"),
          h(Select, { value: status || "all", onValueChange: (value) => setStatus(value === "all" ? "" : value) },
            option("all", "All visible"),
            STATUSES.map((s) => option(s, s)),
          ),
        ),
        h("label", { className: "si-check" },
          h(Input, { type: "checkbox", checked: includeArchived, onChange: (e) => setIncludeArchived(e.target.checked) }),
          h("span", null, "include archived/rejected"),
        ),
        h(Button, { variant: "secondary", onClick: loadBoard, disabled: loading }, loading ? "Refreshing…" : "Refresh"),
      ),
      error && h("div", { className: "si-error", role: "alert" }, error),
      project && h("section", { className: "si-project-note" },
        h("strong", null, project.name),
        h("span", null, `Kanban board: ${project.kanban_board || project.slug}`),
        h("span", null, `Workspace: ${project.workspace_kind || "scratch"}${project.workspace_path ? ` · ${project.workspace_path}` : ""}`),
      ),
      h("div", { className: "si-layout" },
        h("aside", { className: "si-sidebar-panel" },
          h("h2", null, "Runs"),
          h(RunList, { runs, selectedRun, onSelectRun: setSelectedRun }),
        ),
        h("main", { className: "si-board" },
          loading && h("div", { className: "si-loading" }, "Loading proposals…"),
          !loading && columns.length === 0 && h("div", { className: "si-empty" },
            h("h2", null, "No cards yet"),
            h("p", null, "Ingest a strict proposal JSON block from a cron prong to populate this board."),
          ),
          !loading && columns.length > 0 && h("div", { className: "si-columns" },
            columns.map((column) => h("section", { key: column.prong.slug, className: "si-column" },
              h("div", { className: "si-column-header" },
                h("h2", null, column.prong.label || column.prong.slug),
                h("span", null, String(column.cards.length)),
              ),
              column.cards.map((card) => h(CardTile, {
                key: card.id,
                card,
                busyId,
                onOpen: openCard,
                onApprove: (c) => act("approve", c),
                onReject: (c) => act("reject", c),
              })),
            )),
          ),
        ),
      ),
      h(DetailDrawer, {
        card: active,
        busyId,
        onClose: () => setActive(null),
        onApprove: (c) => act("approve", c),
        onReject: (c) => act("reject", c),
      }),
    );
  }

  window.__HERMES_PLUGINS__.register("sligo", SelfImprovementApp);
})();
