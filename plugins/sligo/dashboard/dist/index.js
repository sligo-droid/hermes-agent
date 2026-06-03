(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const hooks = SDK.hooks;
  const C = SDK.components || {};
  const Button = C.Button || "button";
  const Spinner = C.Spinner || function Spinner() { return h("span", { className: "sligo-spinner" }, "Loading"); };
  const fetchJSON = SDK.fetchJSON;

  const API = "/api/plugins/sligo";
  const STATUS_OPTIONS = [
    { value: "proposed", label: "Proposed" },
    { value: "approved", label: "Approved" },
    { value: "in_progress", label: "In progress" },
    { value: "completed", label: "Completed" },
    { value: "failed", label: "Failed" },
    { value: "rejected", label: "Rejected" },
    { value: "all", label: "All statuses" },
  ];

  function api(path, options) {
    return fetchJSON(API + path, options);
  }

  function errorText(err) {
    const raw = err && err.message ? String(err.message) : String(err || "Unknown error");
    const match = raw.match(/^\d{3}:\s*(.*)$/s);
    const body = match ? match[1] : raw;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch (_err) {}
    return body;
  }

  function formatDate(value) {
    if (!value) return "Unknown run date";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  function labelForProject(projects, key) {
    const project = projects.find(function (p) { return p.key === key; });
    return project ? project.name || project.key : key || "Unknown project";
  }

  function labelForProng(projects, projectKey, prongKey) {
    const project = projects.find(function (p) { return p.key === projectKey; });
    const prong = project && (project.prongs || []).find(function (p) { return p.key === prongKey; });
    return prong ? prong.name || prong.key : prongKey || "Unscoped";
  }

  function runForProposal(runsById, proposal) {
    return runsById[String(proposal.run_id)] || null;
  }

  function metadataValue(proposal, key, fallback) {
    const meta = proposal && proposal.parser_metadata;
    if (meta && Object.prototype.hasOwnProperty.call(meta, key)) return meta[key];
    return fallback;
  }

  function splitLines(value) {
    if (Array.isArray(value)) return value.map(String).filter(Boolean);
    if (!value) return [];
    return String(value).split(/\n+/).map(function (line) { return line.replace(/^[-*]\s*/, "").trim(); }).filter(Boolean);
  }

  function sourceHref(proposal, run) {
    return proposal.source_url || (run && run.source_url) || proposal.source_output_url || (run && run.source_output_url) || "";
  }

  function sourceOutputUrl(proposal, run) {
    return proposal.source_output_url || (run && run.source_output_url) || "";
  }

  function sameOriginUrl(path) {
    if (!path) return "";
    if (/^[a-z][a-z0-9+.-]*:/i.test(path)) return path;
    const rawBase = window.__HERMES_BASE_PATH__ || "";
    const base = rawBase ? (rawBase.charAt(0) === "/" ? rawBase : "/" + rawBase).replace(/\/+$/, "") : "";
    const suffix = path.charAt(0) === "/" ? path : "/" + path;
    return new URL(base + suffix, window.location.origin).toString();
  }

  function authenticatedFetch(path) {
    const headers = new Headers({ "Accept": "text/markdown" });
    if (window.__HERMES_SESSION_TOKEN__) headers.set("X-Hermes-Session-Token", window.__HERMES_SESSION_TOKEN__);
    return fetch(sameOriginUrl(path), { headers: headers, credentials: "include" });
  }

  function openMarkdownBlob(text, filename) {
    const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    if (!opened) {
      const link = document.createElement("a");
      link.href = url;
      link.download = filename || "sligo-source-output.md";
      document.body.appendChild(link);
      link.click();
      link.remove();
    }
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
  }

  function StatusPill(props) {
    return h("span", { className: "sligo-pill sligo-pill--" + String(props.status || "unknown").replace(/[^a-z0-9_-]/gi, "-") }, props.children || props.status || "unknown");
  }

  function Field(props) {
    return h("label", { className: "sligo-field" },
      h("span", null, props.label),
      props.children,
    );
  }

  function EmptyState(props) {
    return h("div", { className: "sligo-empty" },
      h("h3", null, props.title),
      h("p", null, props.children),
      props.action || null,
    );
  }

  function ProposalCard(props) {
    const proposal = props.proposal;
    const run = props.run;
    const worker = proposal.worker || {};
    const confidence = proposal.confidence || metadataValue(proposal, "confidence", "unscored");
    const effort = proposal.estimated_effort || metadataValue(proposal, "estimated_effort", metadataValue(proposal, "effort", "not estimated"));
    return h("button", {
      className: "sligo-card" + (props.selected ? " sligo-card--selected" : ""),
      type: "button",
      onClick: function () { props.onSelect(proposal.id); },
    },
      h("div", { className: "sligo-card__top" },
        h(StatusPill, { status: proposal.status }, proposal.status || "proposed"),
        h("span", { className: "sligo-card__date" }, formatDate(run && (run.completed_at || run.created_at || run.started_at))),
      ),
      h("h3", null, proposal.title || "Untitled proposal"),
      h("p", null, proposal.summary || proposal.body || "No one-sentence summary was supplied by the parser."),
      h("div", { className: "sligo-card__meta" },
        h("span", null, props.projectLabel),
        h("span", null, props.prongLabel),
        h("span", null, "Priority: " + (proposal.priority || "normal")),
        h("span", null, "Confidence: " + confidence),
        h("span", null, "Effort: " + effort),
      ),
      h("div", { className: "sligo-card__footer" },
        proposal.rejected_at ? h("span", null, "Rejected by " + (proposal.rejected_by || "operator")) : null,
        proposal.approved_at ? h("span", null, "Approved by " + (proposal.approved_by || "operator")) : null,
        worker.url ? h("a", { href: worker.url, onClick: function (event) { event.stopPropagation(); } }, "Worker link") : h("span", null, "No worker link"),
      ),
    );
  }

  function DetailDrawer(props) {
    const proposal = props.proposal;
    const run = props.run;
    const state = hooks.useState("");
    const note = state[0];
    const setNote = state[1];
    hooks.useEffect(function () {
      if (!proposal) return;
      setNote("");
    }, [proposal && proposal.id]);

    if (!proposal) return null;
    const worker = proposal.worker || {};
    const evidence = splitLines(proposal.evidence || proposal.evidence_bullets);
    const acceptance = splitLines(proposal.acceptance_criteria);
    const prompt = proposal.worker_prompt || proposal.proposed_worker_prompt || "The backend will generate the worker prompt from stored proposal fields and project/prong configuration when approved.";
    const source = sourceHref(proposal, run);
    const outputUrl = sourceOutputUrl(proposal, run);
    const history = Array.isArray(proposal.audit_log) ? proposal.audit_log : [];

    function mutate(action, payload) {
      props.onAction(proposal.id, action, payload || {});
    }

    function openSourceOutput() {
      props.onSourceError("");
      authenticatedFetch(outputUrl)
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.text();
        })
        .then(function (text) { openMarkdownBlob(text, "sligo-source-output-" + proposal.id + ".md"); })
        .catch(function (err) { props.onSourceError("Could not open source output: " + errorText(err)); });
    }

    return h("aside", { className: "sligo-drawer", "aria-label": "Proposal detail" },
      h("div", { className: "sligo-drawer__header" },
        h("div", null,
          h("p", { className: "sligo-eyebrow" }, "Proposal review"),
          h("h2", null, proposal.title || "Untitled proposal"),
        ),
        h("button", { className: "sligo-icon-button", type: "button", onClick: props.onClose, "aria-label": "Close proposal detail" }, "x"),
      ),
      props.actionError ? h("div", { className: "sligo-error", role: "alert" }, props.actionError) : null,
      props.sourceError ? h("div", { className: "sligo-error", role: "alert" }, props.sourceError) : null,
      h("section", null,
        h("h3", null, "Rationale"),
        h("p", null, proposal.rationale || proposal.body || "No generated rationale is available from this proposal."),
      ),
      h("section", null,
        h("h3", null, "Recommendation"),
        h("dl", { className: "sligo-dl" },
          h("dt", null, "Action"), h("dd", null, proposal.recommended_action || "No recommended action was exposed by the API."),
          h("dt", null, "Risk notes"), h("dd", null, proposal.risk_notes || "No risk notes were supplied."),
          h("dt", null, "Confidence"), h("dd", null, proposal.confidence || "unscored"),
          h("dt", null, "Estimated effort"), h("dd", null, proposal.estimated_effort || "not estimated"),
        ),
      ),
      h("section", null,
        h("h3", null, "Evidence"),
        evidence.length ? h("ul", null, evidence.slice(0, 8).map(function (item, index) { return h("li", { key: index }, item); })) : h("p", { className: "sligo-muted" }, "No evidence bullets were exposed by the API for this card."),
      ),
      h("section", null,
        h("h3", null, "Acceptance Criteria"),
        acceptance.length ? h("ul", null, acceptance.map(function (item, index) { return h("li", { key: index }, item); })) : h("p", { className: "sligo-muted" }, "No acceptance criteria were exposed by the API for this card."),
      ),
      h("section", null,
        h("h3", null, "Proposed Worker Prompt"),
        h("pre", null, prompt),
      ),
      h("section", null,
        h("h3", null, "Source Run"),
        h("dl", { className: "sligo-dl" },
          h("dt", null, "Run"), h("dd", null, run ? "#" + run.id + " " + (run.source_title || run.source_type || "proposal run") : "Unknown"),
          h("dt", null, "Date"), h("dd", null, formatDate(run && (run.completed_at || run.created_at || run.started_at))),
          h("dt", null, "Parser"), h("dd", null, proposal.parser_name || (run && run.parser_name) || "unknown"),
          h("dt", null, "Parse status"), h("dd", null, (run && (run.parse_status || run.status)) || "unknown"),
        ),
        outputUrl ? h(Button, { type: "button", onClick: openSourceOutput }, "Open source output") : source ? h("p", { className: "sligo-muted" }, "Source URL: ", h("code", null, source)) : h("p", { className: "sligo-muted" }, "No source output link is present on this proposal."),
      ),
      h("section", null,
        h("h3", null, "Feedback & Action History"),
        history.length ? h("ul", null, history.map(function (item, index) { return h("li", { key: index }, [formatDate(item.at), item.action, item.actor ? "by " + item.actor : "", item.note].filter(Boolean).join(" - ")); })) : h("p", { className: "sligo-muted" }, "No operator actions have been recorded yet."),
      ),
      h("section", null,
        h("h3", null, "Linked Worker"),
        worker.task_id ? h("dl", { className: "sligo-dl" },
          h("dt", null, "Board"), h("dd", null, worker.board || "unknown"),
          h("dt", null, "Task"), h("dd", null, worker.task_id),
          h("dt", null, "Status"), h("dd", null, worker.status || "unknown"),
        ) : h("p", { className: "sligo-muted" }, "This proposal has not created a worker task yet."),
        worker.url ? h("a", { href: worker.url }, "Open worker") : null,
      ),
      h("section", { className: "sligo-actions" },
        h("h3", null, "Decision Actions"),
        h("textarea", { value: note, onChange: function (event) { setNote(event.target.value); }, placeholder: "Reason or feedback for this action" }),
        h("div", { className: "sligo-action-row" },
          h(Button, { disabled: props.busy, onClick: function () { mutate("approve", { reason: note, feedback: note }); } }, props.busy === "approve" ? "Approving..." : proposal.status === "approved" ? "Approved" : "Approve"),
          h(Button, { disabled: props.busy, onClick: function () { mutate("reject", { reason: note || "Rejected from dashboard", feedback: note }); } }, props.busy === "reject" ? "Rejecting..." : "Reject"),
          h(Button, { disabled: props.busy || !note.trim(), onClick: function () { mutate("feedback", { reason: "operator feedback", feedback: note }); } }, props.busy === "feedback" ? "Saving..." : "Send Feedback"),
        ),
      ),
    );
  }

  function SligoPage() {
    const state = hooks.useState({ loading: true, error: "", projects: [], runs: [], proposals: [] });
    const data = state[0];
    const setData = state[1];
    const filtersState = hooks.useState({ project: "", prong: "", status: "proposed", run: "", date: "" });
    const filters = filtersState[0];
    const setFilters = filtersState[1];
    const selectedState = hooks.useState(null);
    const selectedId = selectedState[0];
    const setSelectedId = selectedState[1];
    const actionState = hooks.useState({ busy: "", error: "" });
    const action = actionState[0];
    const setAction = actionState[1];
    const sourceErrorState = hooks.useState("");
    const sourceError = sourceErrorState[0];
    const setSourceError = sourceErrorState[1];

    const runsById = hooks.useMemo(function () {
      const out = {};
      data.runs.forEach(function (run) { out[String(run.id)] = run; });
      return out;
    }, [data.runs]);

    const selectedProject = hooks.useMemo(function () {
      return data.projects.find(function (project) { return project.key === filters.project; }) || null;
    }, [data.projects, filters.project]);

    const selectedProposal = hooks.useMemo(function () {
      return data.proposals.find(function (proposal) { return proposal.id === selectedId; }) || null;
    }, [data.proposals, selectedId]);

    const visible = hooks.useMemo(function () {
      return data.proposals.filter(function (proposal) {
        if (filters.run && String(proposal.run_id) !== filters.run) return false;
        if (filters.date) {
          const run = runForProposal(runsById, proposal);
          const sourceDate = (run && (run.completed_at || run.created_at || run.started_at)) || proposal.created_at || "";
          if (!sourceDate.startsWith(filters.date)) return false;
        }
        if (filters.status !== "all" && filters.status === "rejected" && proposal.status !== "rejected") return false;
        if (filters.status !== "all" && filters.status !== "rejected" && proposal.status !== filters.status) return false;
        return true;
      });
    }, [data.proposals, filters.run, filters.date, filters.status, runsById]);

    const load = hooks.useCallback(function () {
      setData(function (prev) { return Object.assign({}, prev, { loading: true, error: "" }); });
      return api("/projects")
        .then(function (projectsResponse) {
          const projects = projectsResponse.projects || [];
          const project = filters.project || (projects[0] && projects[0].key) || "";
          const params = new URLSearchParams();
          if (project) params.set("project", project);
          if (filters.prong) params.set("prong", filters.prong);
          if (filters.status === "all" || filters.status === "rejected") params.set("include_inactive", "true");
          else params.set("status", filters.status || "proposed");
          params.set("limit", "200");
          const runParams = new URLSearchParams();
          if (project) runParams.set("project", project);
          if (filters.prong) runParams.set("prong", filters.prong);
          runParams.set("limit", "200");
          return Promise.all([
            Promise.resolve(projects),
            api("/proposals?" + params.toString()),
            api("/runs?" + runParams.toString()),
          ]);
        })
        .then(function (result) {
          const projects = result[0];
          const proposals = result[1].proposals || [];
          const runs = result[2].runs || [];
          const nextProject = filters.project || (projects[0] && projects[0].key) || "";
          if (!filters.project && nextProject) setFilters(function (prev) { return Object.assign({}, prev, { project: nextProject }); });
          setData({ loading: false, error: "", projects: projects, proposals: proposals, runs: runs });
          if (selectedId && !proposals.some(function (proposal) { return proposal.id === selectedId; })) setSelectedId(null);
        })
        .catch(function (err) { setData(function (prev) { return Object.assign({}, prev, { loading: false, error: errorText(err) }); }); });
    }, [filters.project, filters.prong, filters.status]);

    hooks.useEffect(function () { load(); }, [load]);

    function updateFilter(key, value) {
      setFilters(function (prev) {
        const next = Object.assign({}, prev, { [key]: value });
        if (key === "project") {
          next.prong = "";
          next.run = "";
        }
        return next;
      });
    }

    function onAction(id, name, payload) {
      setAction({ busy: name, error: "" });
      return api("/proposals/" + id + "/" + name, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      })
        .then(function (response) {
          const next = response.proposal;
          setData(function (prev) {
            return Object.assign({}, prev, {
              proposals: prev.proposals.map(function (proposal) { return proposal.id === next.id ? next : proposal; }),
            });
          });
          setAction({ busy: "", error: "" });
        })
        .catch(function (err) { setAction({ busy: "", error: errorText(err) }); });
    }

    const defaultRejectedNote = filters.status === "proposed" ? "Rejected cards are hidden from this default view. Choose Rejected or All statuses to inspect them." : "";

    return h("div", { className: "sligo" },
      h("header", { className: "sligo-hero" },
        h("div", null,
          h("p", { className: "sligo-eyebrow" }, "Sligo operator console"),
          h("h1", null, "Review self-improvement proposals"),
          h("p", null, "Select a project and run, inspect generated rationale, then approve, reject, or send feedback into the authenticated Sligo workflow."),
        ),
        h("div", { className: "sligo-hero__stats" },
          h("span", null, String(visible.length)),
          h("small", null, "visible proposals"),
        ),
      ),
      h("div", { className: "sligo-filters" },
        h(Field, { label: "Project" }, h("select", { value: filters.project, onChange: function (event) { updateFilter("project", event.target.value); } }, data.projects.map(function (project) { return h("option", { key: project.key, value: project.key }, project.name || project.key); }))),
        h(Field, { label: "Prong" }, h("select", { value: filters.prong, onChange: function (event) { updateFilter("prong", event.target.value); } }, [h("option", { key: "", value: "" }, "All prongs")].concat((selectedProject && selectedProject.prongs || []).map(function (prong) { return h("option", { key: prong.key, value: prong.key }, prong.name || prong.key); })))),
        h(Field, { label: "Status" }, h("select", { value: filters.status, onChange: function (event) { updateFilter("status", event.target.value); } }, STATUS_OPTIONS.map(function (option) { return h("option", { key: option.value, value: option.value }, option.label); }))),
        h(Field, { label: "Run" }, h("select", { value: filters.run, onChange: function (event) { updateFilter("run", event.target.value); } }, [h("option", { key: "", value: "" }, "All runs")].concat(data.runs.map(function (run) { return h("option", { key: run.id, value: String(run.id) }, "#" + run.id + " " + (run.source_title || run.source_type || formatDate(run.created_at))); })))),
        h(Field, { label: "Date" }, h("input", { type: "date", value: filters.date, onChange: function (event) { updateFilter("date", event.target.value); } })),
        h(Button, { onClick: load, disabled: data.loading }, data.loading ? "Refreshing..." : "Refresh"),
      ),
      data.error ? h("div", { className: "sligo-error", role: "alert" }, data.error, h("button", { type: "button", onClick: load }, "Retry")) : null,
      defaultRejectedNote ? h("p", { className: "sligo-muted sligo-default-note" }, defaultRejectedNote) : null,
      data.loading ? h("div", { className: "sligo-loading" }, h(Spinner, null), h("span", null, "Loading Sligo proposals")) : null,
      !data.loading && !data.error && visible.length === 0 ? h(EmptyState, {
        title: filters.status === "rejected" ? "No rejected proposals match these filters" : "No proposals match these filters",
        action: h(Button, { onClick: function () { setFilters(Object.assign({}, filters, { status: "all", run: "", date: "" })); } }, "Show all history"),
      }, data.runs.some(function (run) { return run.parse_status === "failed"; }) ? "One or more runs reported parser failures. Select a run from history or inspect cron output metadata." : "Try another project, prong, status, date, or run history selection.") : null,
      h("main", { className: "sligo-layout" },
        h("section", { className: "sligo-list" }, visible.map(function (proposal) {
          return h(ProposalCard, {
            key: proposal.id,
            proposal: proposal,
            run: runForProposal(runsById, proposal),
            selected: selectedId === proposal.id,
            onSelect: setSelectedId,
            projectLabel: labelForProject(data.projects, proposal.project_key),
            prongLabel: labelForProng(data.projects, proposal.project_key, proposal.prong_key),
          });
        })),
        h(DetailDrawer, {
          proposal: selectedProposal,
          run: selectedProposal ? runForProposal(runsById, selectedProposal) : null,
          busy: action.busy,
          actionError: action.error,
          sourceError: sourceError,
          onAction: onAction,
          onSourceError: setSourceError,
          onClose: function () { setSelectedId(null); },
        }),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("sligo", SligoPage);
})();
