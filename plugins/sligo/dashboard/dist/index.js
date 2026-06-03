(function () {
  const sdk = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!sdk || !registry) return;

  const React = sdk.React;
  const { useEffect, useMemo, useState } = sdk.hooks;
  const { fetchJSON } = sdk;
  const { cn, timeAgo } = sdk.utils;
  const { Button } = sdk.components;
  const h = React.createElement;
  const API = "/api/plugins/sligo";

  const statusOptions = [
    { value: "active", label: "Active" },
    { value: "proposed", label: "Proposed" },
    { value: "approved", label: "Approved" },
    { value: "rejected", label: "Rejected history" },
    { value: "all", label: "All" },
  ];

  function SelfImprovementDashboard() {
    const [projects, setProjects] = useState([]);
    const [runs, setRuns] = useState([]);
    const [proposals, setProposals] = useState([]);
    const [filters, setFilters] = useState({ project: "all", run: "all", status: "active" });
    const [selectedId, setSelectedId] = useState(null);
    const [detail, setDetail] = useState(null);
    const [loading, setLoading] = useState(true);
    const [detailLoading, setDetailLoading] = useState(false);
    const [action, setAction] = useState(null);
    const [error, setError] = useState("");
    const [editOpen, setEditOpen] = useState(false);
    const [edit, setEdit] = useState({ title: "", summary: "", body: "", priority: "medium", confidence: "0.8", effort: "small", acceptance: "" });
    const [rejectReason, setRejectReason] = useState("");

    function loadBoard() {
      setLoading(true);
      setError("");
      return Promise.all([
        fetchJSON(`${API}/projects`),
        fetchJSON(`${API}/runs?limit=100`),
        fetchJSON(`${API}/proposals?limit=500`),
      ])
        .then(([projectData, runData, proposalData]) => {
          setProjects(projectData.projects || []);
          setRuns(runData.runs || []);
          setProposals(proposalData.proposals || []);
        })
        .catch((err) => setError(messageFromError(err)))
        .finally(() => setLoading(false));
    }

    useEffect(() => {
      loadBoard();
    }, []);

    useEffect(() => {
      if (!selectedId) {
        setDetail(null);
        return;
      }
      setDetailLoading(true);
      fetchJSON(`${API}/proposals/${encodeURIComponent(selectedId)}`)
        .then((data) => {
          const proposal = data.proposal || null;
          setDetail(proposal);
          if (proposal) {
            setEdit({
              title: proposal.title || "",
              summary: proposal.summary || "",
              body: proposal.body || "",
              priority: proposal.priority || "medium",
              confidence: String(proposal.confidence ?? ""),
              effort: proposal.effort || "small",
              acceptance: (proposal.acceptance_criteria || []).join("\n"),
            });
          }
        })
        .catch((err) => setError(messageFromError(err)))
        .finally(() => setDetailLoading(false));
    }, [selectedId]);

    const prongLabels = useMemo(() => {
      const labels = new Map();
      for (const project of projects) {
        for (const prong of project.prongs || []) {
          labels.set(`${project.id}:${prong.id}`, `${project.name || project.id} / ${prong.name || prong.id}`);
        }
      }
      return labels;
    }, [projects]);

    const filtered = useMemo(() => {
      return proposals.filter((proposal) => {
        if (filters.project !== "all" && proposal.project !== filters.project) return false;
        if (filters.status === "active" && proposal.status === "rejected") return false;
        if (filters.status !== "active" && filters.status !== "all" && proposal.status !== filters.status) return false;
        if (filters.run !== "all" && String(proposal.run_id) !== filters.run) return false;
        return true;
      });
    }, [filters, proposals]);

    const columns = useMemo(() => {
      const byProng = new Map();
      for (const project of projects) {
        if (filters.project !== "all" && project.id !== filters.project) continue;
        for (const prong of project.prongs || []) {
          byProng.set(`${project.id}:${prong.id}`, { key: `${project.id}:${prong.id}`, project: project.id, prong: prong.id, label: `${project.name || project.id} / ${prong.name || prong.id}`, cards: [] });
        }
      }
      for (const proposal of filtered) {
        const key = `${proposal.project}:${proposal.prong}`;
        if (!byProng.has(key)) byProng.set(key, { key, project: proposal.project, prong: proposal.prong, label: prongLabels.get(key) || `${proposal.project} / ${proposal.prong}`, cards: [] });
        byProng.get(key).cards.push(proposal);
      }
      return Array.from(byProng.values()).filter((column) => column.cards.length > 0 || filters.project !== "all");
    }, [filtered, filters.project, prongLabels, projects]);

    const stats = useMemo(() => {
      return proposals.reduce((acc, p) => {
        acc[p.status] = (acc[p.status] || 0) + 1;
        return acc;
      }, {});
    }, [proposals]);

    function setFilter(name, value) {
      setFilters((prev) => ({ ...prev, [name]: value }));
    }

    function refreshSelected(nextProposal) {
      return loadBoard().then(() => {
        if (nextProposal) {
          setDetail(nextProposal);
          setSelectedId(nextProposal.card_id);
        } else if (selectedId) {
          return fetchJSON(`${API}/proposals/${encodeURIComponent(selectedId)}`)
            .then((data) => setDetail(data.proposal || null))
            .catch(() => setDetail(null));
        }
      });
    }

    function runAction(name, fn) {
      setAction(name);
      setError("");
      return fn()
        .then((data) => refreshSelected(data.proposal))
        .catch((err) => setError(messageFromError(err)))
        .finally(() => setAction(null));
    }

    function approve(cardId) {
      return runAction(`approve:${cardId}`, () => fetchJSON(`${API}/proposals/${encodeURIComponent(cardId)}/approve`, { method: "POST" }));
    }

    function reject(cardId) {
      const reason = rejectReason.trim() || window.prompt("Why reject this proposal?") || "Rejected by operator";
      if (!reason.trim()) return Promise.resolve();
      return runAction(`reject:${cardId}`, () => fetchJSON(`${API}/proposals/${encodeURIComponent(cardId)}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }),
      })).then(() => setRejectReason(""));
    }

    function saveEdit(cardId) {
      const acceptance = edit.acceptance.split("\n").map((line) => line.trim()).filter(Boolean);
      return runAction(`edit:${cardId}`, () => fetchJSON(`${API}/proposals/${encodeURIComponent(cardId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: edit.title,
          summary: edit.summary,
          body: edit.body,
          priority: edit.priority,
          confidence: Number(edit.confidence),
          effort: edit.effort,
          acceptance_criteria: acceptance,
        }),
      })).then(() => setEditOpen(false));
    }

    return h("section", { className: "mx-auto flex w-full max-w-[1800px] flex-col gap-4 text-text-primary", "data-testid": "self-improvement-board" },
      h("header", { className: "flex flex-col gap-3 rounded-2xl border border-current/15 bg-background-base/70 p-4 shadow-[0_0_40px_rgba(0,0,0,0.25)] lg:flex-row lg:items-end lg:justify-between" },
        h("div", { className: "space-y-2" },
          h("p", { className: "font-mondwest text-xs uppercase tracking-[0.2em] text-text-tertiary" }, "Sligo operator shell"),
          h("div", null,
            h("h1", { className: "text-2xl font-semibold tracking-tight text-midground sm:text-3xl" }, "Self-Improvement Proposals"),
            h("p", { className: "max-w-3xl text-sm text-text-secondary" }, "Review generated improvement proposals, approve work into the existing Workers board, and keep rejected ideas available through history filters.")
          )
        ),
        h("div", { className: "flex flex-wrap gap-2 text-xs text-text-secondary" },
          statPill("Proposed", stats.proposed || 0),
          statPill("Approved", stats.approved || 0),
          statPill("Rejected", stats.rejected || 0),
          h(Button, { onClick: loadBoard, disabled: loading, className: "h-8" }, loading ? "Refreshing..." : "Refresh")
        )
      ),
      error ? h("div", { role: "alert", className: "rounded-xl border border-red-400/50 bg-red-950/30 p-3 text-sm text-red-100" }, error) : null,
      h("div", { className: "grid gap-3 rounded-2xl border border-current/10 bg-background-base/50 p-3 sm:grid-cols-3" },
        selectFilter("Project", filters.project, (v) => setFilter("project", v), [{ value: "all", label: "All projects" }].concat(projects.map((p) => ({ value: p.id, label: p.name || p.id })))),
        selectFilter("Run / date", filters.run, (v) => setFilter("run", v), [{ value: "all", label: "All runs" }].concat(runs.map((r) => ({ value: String(r.id), label: `${formatDate(r.source_timestamp || r.created_at)} · ${r.project || "project"}/${r.prong || "prong"}` })))),
        selectFilter("Status", filters.status, (v) => setFilter("status", v), statusOptions)
      ),
      loading ? h("div", { className: "rounded-xl border border-current/10 p-6 text-sm text-text-secondary" }, "Loading self-improvement proposals...") : null,
      !loading && columns.length === 0 ? h("div", { className: "rounded-xl border border-current/10 p-6 text-sm text-text-secondary" }, "No proposals match these filters.") : null,
      h("div", { className: "grid items-start gap-4 xl:grid-cols-3 2xl:grid-cols-4" }, columns.map((column) => h("div", { key: column.key, className: "min-w-0 rounded-2xl border border-current/15 bg-black/25 p-3" },
        h("div", { className: "mb-3 flex items-center justify-between gap-2" },
          h("h2", { className: "truncate text-sm font-semibold text-midground" }, column.label),
          h("span", { className: "rounded-full border border-current/15 px-2 py-0.5 text-xs text-text-tertiary" }, String(column.cards.length))
        ),
        h("div", { className: "flex flex-col gap-3" }, column.cards.map((proposal) => h(ProposalCard, {
          key: proposal.card_id,
          proposal,
          prongLabel: prongLabels.get(`${proposal.project}:${proposal.prong}`),
          selected: selectedId === proposal.card_id,
          busy: action === `approve:${proposal.card_id}` || action === `reject:${proposal.card_id}`,
          onOpen: () => setSelectedId(proposal.card_id),
          onApprove: () => approve(proposal.card_id),
          onReject: () => reject(proposal.card_id),
        })))
      ))),
      detail ? h(DetailDrawer, {
        proposal: detail,
        loading: detailLoading,
        action,
        edit,
        editOpen,
        rejectReason,
        setEdit,
        setEditOpen,
        setRejectReason,
        onClose: () => setSelectedId(null),
        onApprove: () => approve(detail.card_id),
        onReject: () => reject(detail.card_id),
        onSave: () => saveEdit(detail.card_id),
      }) : null
    );
  }

  function ProposalCard({ proposal, prongLabel, selected, busy, onOpen, onApprove, onReject }) {
    const worker = workerLink(proposal);
    return h("article", { className: cn("rounded-xl border bg-background-base/80 p-3 transition hover:border-midground/60", selected ? "border-midground" : "border-current/10"), "data-status": proposal.status },
      h("button", { type: "button", onClick: onOpen, className: "block w-full text-left" },
        h("div", { className: "mb-2 flex items-start justify-between gap-2" },
          h("h3", { className: "line-clamp-2 text-sm font-semibold text-midground" }, proposal.title),
          statusBadge(proposal.status)
        ),
        h("p", { className: "line-clamp-3 text-xs leading-5 text-text-secondary" }, oneSentence(proposal.summary)),
        h("dl", { className: "mt-3 grid grid-cols-2 gap-2 text-[11px] text-text-tertiary" },
          meta("Project/prong", prongLabel || `${proposal.project}/${proposal.prong}`),
          meta("Source", formatDate(proposal.source_timestamp || proposal.created_at)),
          meta("Priority", `${proposal.priority || "n/a"} · ${formatConfidence(proposal.confidence)}`),
          meta("Effort", proposal.effort || "n/a")
        )
      ),
      h("div", { className: "mt-3 flex flex-wrap gap-2" },
        proposal.status === "proposed" ? h(Button, { onClick: onApprove, disabled: busy, className: "h-8 px-3 text-xs" }, busy ? "Working..." : "Approve") : null,
        proposal.status === "proposed" ? h(Button, { ghost: true, onClick: onReject, disabled: busy, className: "h-8 px-3 text-xs" }, "Reject") : null,
        h(Button, { ghost: true, onClick: onOpen, className: "h-8 px-3 text-xs" }, "Details"),
        worker ? h("a", { href: worker.url, className: "inline-flex h-8 items-center rounded-md border border-current/15 px-3 text-xs text-midground hover:bg-midground/10" }, "Worker") : null
      )
    );
  }

  function DetailDrawer(props) {
    const proposal = props.proposal;
    const worker = workerLink(proposal);
    const sourceHref = proposal.source_output_path ? `file://${proposal.source_output_path}` : null;
    return h("div", { className: "fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-sm", role: "dialog", "aria-modal": "true" },
      h("div", { className: "flex h-full w-full max-w-3xl flex-col overflow-y-auto border-l border-current/20 bg-background-base p-4 shadow-2xl sm:p-6" },
        h("div", { className: "mb-4 flex items-start justify-between gap-3" },
          h("div", null,
            h("p", { className: "font-mondwest text-xs uppercase tracking-[0.18em] text-text-tertiary" }, `${proposal.project} / ${proposal.prong}`),
            h("h2", { className: "text-xl font-semibold text-midground" }, proposal.title),
            h("div", { className: "mt-2 flex flex-wrap gap-2" }, statusBadge(proposal.status), statusBadge(proposal.lifecycle_status || "new"))
          ),
          h(Button, { ghost: true, onClick: props.onClose, className: "h-8" }, "Close")
        ),
        props.loading ? h("p", { className: "text-sm text-text-secondary" }, "Loading detail...") : null,
        h("div", { className: "mb-4 flex flex-wrap gap-2" },
          proposal.status === "proposed" ? h(Button, { onClick: props.onApprove, disabled: props.action === `approve:${proposal.card_id}` }, props.action === `approve:${proposal.card_id}` ? "Approving..." : "Approve into Workers") : null,
          proposal.status === "proposed" ? h(Button, { ghost: true, onClick: props.onReject, disabled: props.action === `reject:${proposal.card_id}` }, props.action === `reject:${proposal.card_id}` ? "Rejecting..." : "Reject") : null,
          proposal.status === "proposed" ? h(Button, { ghost: true, onClick: () => props.setEditOpen(!props.editOpen) }, props.editOpen ? "Cancel edit" : "Edit") : null,
          sourceHref ? h("a", { href: sourceHref, target: "_blank", rel: "noreferrer", className: "inline-flex items-center rounded-md border border-current/15 px-3 py-2 text-sm text-midground hover:bg-midground/10" }, "View source output") : null,
          worker ? h("a", { href: worker.url, className: "inline-flex items-center rounded-md border border-current/15 px-3 py-2 text-sm text-midground hover:bg-midground/10" }, "View worker") : null
        ),
        proposal.status === "proposed" ? h("label", { className: "mb-4 block text-xs text-text-tertiary" }, "Reject reason", h("textarea", { value: props.rejectReason, onChange: (e) => props.setRejectReason(e.target.value), className: inputClass("mt-1 min-h-16"), placeholder: "Optional reason used when rejecting" })) : null,
        props.editOpen ? h(EditForm, props) : null,
        h(Section, { title: "Rationale" }, proposal.body || proposal.summary),
        h(Section, { title: "Evidence" }, renderEvidence(proposal.evidence)),
        h(Section, { title: "Acceptance criteria" }, listOrEmpty(proposal.acceptance_criteria)),
        h(Section, { title: "Generated worker prompt" }, proposal.worker_prompt),
        h(Section, { title: "Source output" }, h("div", { className: "space-y-1" },
          h("p", null, proposal.source_output_path || "No source output path recorded."),
          proposal.source_output_sha256 ? h("p", { className: "break-all text-text-tertiary" }, `sha256: ${proposal.source_output_sha256}`) : null,
          h("p", { className: "text-text-tertiary" }, `Run ${proposal.run_id || "n/a"} · ${formatDate(proposal.source_timestamp || proposal.created_at)}`)
        )),
        h(Section, { title: "Feedback and action history" }, renderFeedback(proposal.feedback || [])),
        h(Section, { title: "Linked worker" }, worker ? h("div", { className: "space-y-1" },
          h("p", null, `Board: ${worker.board || proposal.resolved_board || "default"}`),
          h("p", null, `Task: ${worker.task_id}`),
          h("a", { href: worker.url, className: "text-midground underline" }, worker.url)
        ) : "No worker has been created yet.")
      )
    );
  }

  function EditForm(props) {
    const edit = props.edit;
    const set = (name, value) => props.setEdit((prev) => ({ ...prev, [name]: value }));
    return h("div", { className: "mb-4 rounded-xl border border-current/15 p-3" },
      h("div", { className: "grid gap-3" },
        field("Title", h("input", { value: edit.title, onChange: (e) => set("title", e.target.value), className: inputClass() })),
        field("Summary", h("textarea", { value: edit.summary, onChange: (e) => set("summary", e.target.value), className: inputClass("min-h-20") })),
        field("Body", h("textarea", { value: edit.body, onChange: (e) => set("body", e.target.value), className: inputClass("min-h-28") })),
        h("div", { className: "grid gap-3 sm:grid-cols-3" },
          field("Priority", h("select", { value: edit.priority, onChange: (e) => set("priority", e.target.value), className: inputClass() }, ["low", "medium", "high", "urgent"].map((v) => h("option", { key: v, value: v }, v)))),
          field("Confidence", h("input", { value: edit.confidence, onChange: (e) => set("confidence", e.target.value), className: inputClass(), inputMode: "decimal" })),
          field("Effort", h("select", { value: edit.effort, onChange: (e) => set("effort", e.target.value), className: inputClass() }, ["small", "medium", "large"].map((v) => h("option", { key: v, value: v }, v))))
        ),
        field("Acceptance criteria", h("textarea", { value: edit.acceptance, onChange: (e) => set("acceptance", e.target.value), className: inputClass("min-h-24"), placeholder: "One criterion per line" })),
        h("div", { className: "flex gap-2" }, h(Button, { onClick: props.onSave, disabled: props.action === `edit:${props.proposal.card_id}` }, props.action === `edit:${props.proposal.card_id}` ? "Saving..." : "Save edits"))
      )
    );
  }

  function Section({ title, children }) {
    return h("section", { className: "mb-4 rounded-xl border border-current/10 bg-black/15 p-3" },
      h("h3", { className: "mb-2 text-sm font-semibold text-midground" }, title),
      typeof children === "string" ? h("p", { className: "whitespace-pre-wrap text-sm leading-6 text-text-secondary" }, children || "None recorded.") : children
    );
  }

  function selectFilter(label, value, onChange, options) {
    return h("label", { className: "text-xs font-medium text-text-secondary" }, label,
      h("select", { value, onChange: (e) => onChange(e.target.value), className: inputClass("mt-1") }, options.map((option) => h("option", { key: option.value, value: option.value }, option.label)))
    );
  }

  function field(label, child) {
    return h("label", { className: "text-xs font-medium text-text-secondary" }, label, h("div", { className: "mt-1" }, child));
  }

  function inputClass(extra) {
    return cn("w-full rounded-lg border border-current/15 bg-black/30 px-3 py-2 text-sm text-text-primary outline-none focus:border-midground", extra || "");
  }

  function statPill(label, value) {
    return h("span", { className: "rounded-full border border-current/15 px-3 py-1" }, `${label}: ${value}`);
  }

  function statusBadge(status) {
    return h("span", { className: cn("shrink-0 rounded-full border px-2 py-0.5 text-[11px] capitalize", status === "approved" ? "border-emerald-400/40 text-emerald-200" : status === "rejected" ? "border-red-400/40 text-red-200" : "border-amber-400/40 text-amber-100") }, status || "unknown");
  }

  function meta(label, value) {
    return h("div", { className: "min-w-0" }, h("dt", { className: "uppercase tracking-[0.12em] text-text-tertiary" }, label), h("dd", { className: "truncate text-text-secondary" }, value || "n/a"));
  }

  function listOrEmpty(items) {
    const list = Array.isArray(items) ? items.filter(Boolean) : [];
    if (!list.length) return "None recorded.";
    return h("ul", { className: "list-disc space-y-1 pl-5 text-sm text-text-secondary" }, list.map((item, idx) => h("li", { key: idx }, String(item))));
  }

  function renderEvidence(evidence) {
    const rows = Array.isArray(evidence) ? evidence : [];
    if (!rows.length) return "No evidence recorded.";
    return h("div", { className: "space-y-2 text-sm text-text-secondary" }, rows.map((item, idx) => h("div", { key: idx, className: "rounded-lg border border-current/10 p-2" },
      h("p", { className: "font-medium text-text-primary" }, String(item.label || item.source || `Evidence ${idx + 1}`)),
      h("p", { className: "whitespace-pre-wrap" }, String(item.detail || item.body || item.summary || JSON.stringify(item)))
    )));
  }

  function renderFeedback(feedback) {
    if (!feedback.length) return "No feedback yet.";
    return h("ol", { className: "space-y-2 text-sm text-text-secondary" }, feedback.map((item) => h("li", { key: item.id, className: "rounded-lg border border-current/10 p-2" },
      h("div", { className: "mb-1 flex flex-wrap justify-between gap-2 text-xs text-text-tertiary" }, h("span", null, `${item.feedback_type || "comment"} · ${item.author || "unknown"}`), h("span", null, formatDate(item.created_at))),
      h("p", { className: "whitespace-pre-wrap" }, item.body)
    )));
  }

  function workerLink(proposal) {
    if (proposal.worker && proposal.worker.url) return proposal.worker;
    if (!proposal.linked_kanban_task_id) return null;
    const board = proposal.linked_kanban_board || proposal.resolved_board || "default";
    const task = proposal.linked_kanban_task_id;
    const session = proposal.linked_worker_run_id;
    return { board, task_id: task, url: session ? `/workers/${encodeURIComponent(session)}/tickets/${encodeURIComponent(task)}` : `/workers?board=${encodeURIComponent(board)}&task=${encodeURIComponent(task)}` };
  }

  function oneSentence(text) {
    const value = String(text || "").trim();
    const match = value.match(/^(.+?[.!?])(?:\s|$)/);
    return match ? match[1] : value;
  }

  function formatConfidence(value) {
    const n = Number(value);
    return Number.isFinite(n) ? `${Math.round(n * 100)}% confidence` : "confidence n/a";
  }

  function formatDate(value) {
    if (!value) return "No date";
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return String(value);
    return timeAgo ? timeAgo(date) : date.toLocaleString();
  }

  function messageFromError(err) {
    return err && err.message ? err.message : String(err || "Unknown error");
  }

  registry.register("sligo", SelfImprovementDashboard);
})();
