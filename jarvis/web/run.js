// Run view: one JARVIS plan on a single screen, in the dense key/value idiom.
//
// Reads only endpoints that already exist (/api/overview, /api/plans/<id>, /api/tasks/<id>)
// and renders them in one grammar: inputs on the left, the subtask DAG as the spine, one
// card per step in dependency order, then run log / dispatch / human gate across the foot.
// Nothing here runs a plan: spending model calls stays behind the Tasks view's armed
// button, so there is exactly one way to start work.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const $ = (s, r = document) => r.querySelector(s);
  const { esc, api, words } = J;

  const STATES = ["pending", "queued", "running", "finished", "blocked", "failed"];
  const PHASE = {
    planned: "await run", running: "executing",
    blocked: "blocked · needs you", finished: "complete",
  };
  let graph = null, planId = null, data = null;

  const hhmm = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const clip = (s, n) => (String(s ?? "").length > n ? String(s).slice(0, n - 1) + "…" : String(s ?? ""));

  // ---------- the one repeated unit ----------
  function card(title, tag, rows, extra = "", state = "") {
    const body = rows.map(([k, v, cls]) =>
      `<dt>${esc(k)}</dt><dd class="${cls || ""}">${esc(v == null ? "—" : String(v))}</dd>`).join("");
    return `<section class="rv-card"${state ? ` data-state="${esc(state)}"` : ""}>
      <h3>${esc(title)}${tag ? `<span class="rv-tag">${esc(tag)}</span>` : ""}</h3>
      <dl class="rv-rows">${body}</dl>${extra}</section>`;
  }
  // A strip of bars, one per item, lit when `on(item)` holds. Real counts only.
  const spark = (items, on) => !items.length ? "" :
    `<div class="rv-spark" aria-hidden="true">${items.map((it) =>
      `<i class="${on(it) ? "on" : ""}" style="height:${on(it) ? 100 : 34}%"></i>`).join("")}</div>`;

  // ---------- data ----------
  async function load(id) {
    const overview = J.state.overview || await api("/api/overview");
    const plans = overview.plans || [];
    const chosen = id || planId || plans[0]?.id;
    if (!chosen) return { overview, plan: null, events: [] };
    const plan = await api("/api/plans/" + encodeURIComponent(chosen));
    // Events come per task; only the steps that actually dispatched have one. Cap the
    // fan-out so opening the view never turns into a dozen round trips.
    const withTask = plan.steps.filter((s) => s.task).slice(-8);
    const fetched = await Promise.all(withTask.map((s) =>
      api("/api/tasks/" + encodeURIComponent(s.task)).then((t) => ({ step: s, task: t })).catch(() => null)));
    const events = [];
    for (const hit of fetched) {
      if (!hit) continue;
      for (const ev of hit.task.events || []) {
        events.push({ at: ev.at, kind: ev.kind, step: hit.step.id, specialist: hit.step.specialist });
      }
    }
    events.sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
    return { overview, plan, events };
  }

  // ---------- bands ----------
  function head(plan, overview, counts) {
    const model = (overview.models?.claude?.high?.model) || overview.brain?.name || "—";
    const edges = plan ? plan.steps.reduce((n, s) => n + (s.depends_on?.length || 0), 0) : 0;
    const roster = plan ? new Set(plan.steps.map((s) => s.specialist)).size : 0;
    const lanes = overview.orchestration?.lanes ?? 1;
    return `<div class="rv-head">
      <span>plan</span><b>${esc(plan ? plan.id : "none")}</b>
      <span>steps ${counts.total}</span><span>edges ${edges}</span>
      <span>specialists ${roster}</span><span>${lanes} lane${lanes === 1 ? "" : "s"}</span>
      <span class="rv-spacer">david / ${esc(model)}</span>
      <span class="rv-state" data-state="${esc(plan?.state || "")}">${esc(plan ? words(plan.state) : "no plan")}</span>
      <span>${esc(hhmm(Date.now()))}</span>
    </div>`;
  }

  function inputs(plan, overview) {
    const r = overview.routing || {}, m = overview.models || {};
    const steps = plan?.steps || [];
    const credits = (J.state.credits || []).find((c) => c.id === "codex");
    const five = (credits?.meters || [])[0];
    const plans = overview.plans || [];
    return [
      card("brief", plan ? plan.id.slice(-4) : "", [
        ["goal", clip(plan?.goal || "—", 22)],
        ["model", m.claude?.high?.model || "—"],
        ["mode", r.auto_escalate ? "auto-escalate" : "owner-routed"],
        ["steps", steps.length ? `${steps.filter((s) => s.state === "finished").length} / ${steps.length}` : "—"],
      ], spark(steps, (s) => s.state === "finished")),
      card("constraints", "", [
        ["writes", overview.desktop ? (r.brain === "claude" ? "claude" : "chatgpt") : "—"],
        ["browser", overview.tools?.claude ? "available" : "off"],
        ["escalate", r.auto_escalate ? "yes" : "no"],
        ["approval", "human", "rv-live"],
      ]),
      card("budget", "", [
        ["codex 5h", five?.pct == null ? "—" : `${Math.round(100 - five.pct)}% left`],
        ["plan", r.simple_power || "low"],
        ["web", r.web_power || "mid"],
        ["calls", String(overview.tasks?.states?.finished ?? 0)],
      ]),
      card("prior_runs", "", [
        ["plans", String(plans.length)],
        ["finished", String(plans.filter((p) => p.state === "finished").length)],
        ["blocked", String(plans.filter((p) => p.state === "blocked").length)],
        ["memory", String(overview.memory?.items ?? 0)],
      ], spark(plans.slice(0, 14), (p) => p.state === "finished")),
    ].join("");
  }

  function stages(plan) {
    if (!plan) {
      return `<div class="rv-card"><div class="rv-empty"><b>No plan yet.</b>
        Ask JARVIS for something big, or pick <b>Plan</b> in the prompt bar. The specialists
        draft one, and it lands here as a screen you can read top to bottom before it runs.</div></div>`;
    }
    // The orchestrator validates that depends_on only names earlier ids, so array order
    // is already a topological order: render it as-is.
    return plan.steps.map((s) => card(s.specialist, s.id, [
      ["state", words(s.state), s.state === "running" ? "rv-live" : s.state === "pending" ? "rv-mute" : ""],
      ["effort", s.power || "mid"],
      ["after", s.depends_on?.length ? s.depends_on.join(" ") : "—", s.depends_on?.length ? "" : "rv-mute"],
      ["task", s.task ? s.task.slice(0, 8) : "—", s.task ? "" : "rv-mute"],
      ["checks", s.acceptance?.length ? String(s.acceptance.length) : "—"],
    ], `<p class="rv-note">${esc(clip(s.objective, 150))}</p>`, s.state)).join("");
  }

  function funnel(plan, counts) {
    const phase = plan ? (PHASE[plan.state] || words(plan.state)) : "idle";
    const flow = [["steps", counts.total], ["finished", counts.finished],
                  ["running", counts.running], ["pending", counts.pending],
                  ["blocked", counts.blocked]]
      .map(([k, v]) => `<b>${v}</b> ${esc(k)}`).join('<span>→</span>');
    return `<div class="rv-funnel"><span class="rv-phase">${esc(phase)}</span>
      <span class="rv-flow">${flow}</span></div>`;
  }

  function foot(plan, events, counts) {
    const log = events.length
      ? `<div class="rv-log">${events.slice(-40).map((e) =>
          `<time>${esc(hhmm(e.at))}</time><span class="rv-actor" data-kind="${esc(e.kind)}">${esc(e.step)}</span>
           <span class="rv-text">${esc(e.specialist)} · ${esc(words(e.kind))}</span>`).join("")}</div>`
      : `<p class="rv-empty">Nothing dispatched yet. Steps log here as workers claim them.</p>`;

    // The orchestrator runs one step at a time and picks the next whose dependencies have
    // all finished, so dispatch is scheduler state: running, ready to claim, or waiting.
    const steps = plan?.steps || [];
    const done = new Set(steps.filter((s) => s.state === "finished").map((s) => s.id));
    const lane = (s) =>
      s.state === "finished" ? "done" :
      s.state === "running" ? "run" :
      s.state === "blocked" || s.state === "failed" ? "ask" :
      (s.depends_on || []).every((d) => done.has(d)) ? "ready" : "waiting";
    const FILL = { done: 100, run: 55, ready: 12, waiting: 0, ask: 100 };
    const order = { run: 0, ask: 1, ready: 2, waiting: 3, done: 4 };
    const ranked = steps.map((s) => ({ s, l: lane(s) }))
      .sort((a, b) => order[a.l] - order[b.l] || a.s.id.localeCompare(b.s.id));
    const ready = ranked.filter((r) => r.l === "ready").length;
    const lanes = ranked.length
      ? `<div class="rv-lanes">${ranked.map(({ s, l }) =>
          `<span class="rv-w">${esc(s.id)}</span><span class="rv-name">${esc(s.specialist)}</span>
           <span class="rv-st" data-state="${esc(l)}">${esc(l)}</span>
           <span class="rv-bar ${l === "done" ? "done" : ""}"><i style="width:${FILL[l]}%"></i></span>
           <span class="rv-st" data-state="${esc(l)}">${s.task ? esc(s.task.slice(0, 6)) : "—"}</span>`).join("")}</div>`
      : `<p class="rv-empty">No steps to dispatch.</p>`;

    const waiting = !plan ? "Nothing to approve."
      : plan.state === "planned" ? "await your decision · nothing has run"
      : plan.state === "blocked" ? "a step failed · open its task to see why"
      : plan.state === "running" ? "running · each step lands as it finishes"
      : "all steps finished · results are in Memory";
    const gate = `<div class="rv-gate">
      <span class="rv-big">${counts.finished} / ${counts.total}<small>steps verified</small></span>
      <span class="rv-sub">${plan ? esc(clip(plan.summary || plan.goal, 90)) : "—"}</span>
      <span class="${plan && plan.state !== "finished" ? "rv-wait" : "rv-sub"}">${esc(waiting)}</span>
      ${plan && plan.state === "planned"
        ? `<button class="btn ghost sm rv-run" type="button" id="rv-open">Review and run in Tasks</button>` : ""}
    </div>`;

    return `<div class="rv-foot">
      <section class="rv-card"><h3>run log<span class="rv-tag">${events.length}</span></h3>
        <div class="rv-scroll">${log}</div></section>
      <section class="rv-card"><h3>dispatch<span class="rv-tag">${counts.running} running · ${ready} ready</span></h3>
        <div class="rv-scroll">${lanes}</div></section>
      <section class="rv-card rv-gate-card"><h3>output / human gate</h3>${gate}</section>
    </div>`;
  }

  // ---------- the spine: the plan's own dependency graph ----------
  function spine(plan) {
    const canvas = $("#rv-spine-canvas");
    if (!canvas || !plan) return;
    if (!graph) graph = new ForceGraph(canvas, { mini: true });
    graph.setData({
      nodes: plan.steps.map((s) => ({ id: s.id, label: `${s.id} ${s.specialist}`, kind: "specialist" })),
      edges: plan.steps.flatMap((s) => (s.depends_on || []).map((d) => ({ src: d, dst: s.id, rel: "then" }))),
    });
  }

  function tally(plan) {
    const counts = { total: plan?.steps.length || 0 };
    for (const name of STATES) counts[name] = (plan?.steps || []).filter((s) => s.state === name).length;
    return counts;
  }

  function render() {
    const { overview, plan, events } = data;
    const counts = tally(plan);
    const cmd = plan
      ? `jarvis orchestrate run ${plan.id}`
      : `jarvis orchestrate plan "<goal>"`;
    $("#rv").innerHTML = head(plan, overview, counts) +
      `<div class="rv-body">
         <div class="rv-inputs">${inputs(plan, overview)}</div>
         <div class="rv-spine"><canvas id="rv-spine-canvas" aria-label="Dependency graph of this plan's steps"></canvas>
           <span class="rv-spine-label">depends on</span></div>
         <div class="rv-stages">${stages(plan)}</div>
       </div>` +
      funnel(plan, counts) + foot(plan, events, counts) +
      `<div class="rv-cmd"><span class="rv-prompt">~/david $</span><span>${esc(cmd)}</span>
         <span class="rv-caret" aria-hidden="true"></span>
         <span class="rv-right">${plan ? esc(words(plan.state)) : "idle"} · nothing runs from this screen</span></div>`;
    graph = null;  // the old canvas went with the innerHTML
    spine(plan);
    $("#rv-open")?.addEventListener("click", () => { location.hash = "tasks"; J.openPlan(plan.id); });
  }

  async function open(id) {
    const host = $("#rv");
    if (!host) return false;
    try {
      data = await load(id);
      planId = data.plan?.id || null;
    } catch (e) {
      host.innerHTML = `<div class="rv-card"><div class="rv-empty"><b>Couldn't read the run.</b>${esc(e.message)}</div></div>`;
      return false;
    }
    render();
    return true;
  }

  // A plan changing anywhere (the live stream, or another process) redraws this view.
  addEventListener("jarvis:live", (e) => {
    const kind = e.detail?.kind;
    if (!$("#view-run") || $("#view-run").hidden) return;
    if (kind === "plan.updated" || kind === "task.event") open(planId);
  });

  window.JarvisRun = { open };
})();
