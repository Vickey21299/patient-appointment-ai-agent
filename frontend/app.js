// UI wiring only. All agent/backend access goes through ChatAPI (api.js).

const STATE_KIND = {
  COLLECTING: "active", OFFERED: "active", HELD: "active", CONFIRMING: "active", MODIFYING: "active",
  SEARCHING: "transient", COMMITTING: "transient", CANCELLING: "transient", RECONCILING: "transient",
  BOOKED: "stable",
  CANCELLED: "terminal", EXPIRED: "terminal",
  ESCALATED: "escalated",
};
const BADGE_CLASS = { stable: "ok", transient: "warn", escalated: "bad", terminal: "muted", active: "" };
const EV_LEVEL = {
  "escalation.packet": "bad", "budget.exhausted": "bad", "circuit.open": "bad",
  "failure.classified": "warn", "retry.scheduled": "warn", "transition.rejected": "warn", "conflict.detected": "warn",
  "reconcile.start": "warn", "duplicate.detected": "warn", "compensation.run": "warn", "orphan_hold": "warn",
  "patient.nudge_due": "warn", "patient.change": "warn", "state.transition": "info", "reconcile.resolved": "ok",
};

const $ = (id) => document.getElementById(id);
const els = {
  cases: $("cases"), messages: $("messages"), suggest: $("suggest"), options: $("options"), form: $("composer"),
  input: $("input"), send: $("btn-send"), reset: $("reset"), advance: $("btn-advance"), checksBtn: $("btn-checks"),
  title: $("chat-title"), sub: $("chat-sub"), badge: $("state-badge"), stepper: $("stepper"), appt: $("appt"),
  checks: $("checks"), langfuse: $("langfuse"), faults: $("faults"), bookings: $("bookings"),
  events: $("events"), evCount: $("ev-count"), mode: $("mode-badge"), model: $("model"), tracing: $("tracing"),
  history: $("history"),
};
let scenarios = [], scenario = null, session = null, busy = false, readOnly = false;

// Small DOM helper: h("div", {class: "x"}, "text", child) — text is always set safely.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) el.append(c);
  return el;
}
const kindOf = (s) => STATE_KIND[s] || "active";
const stateBadge = (s) => h("span", { class: `badge ${BADGE_CLASS[kindOf(s)]}` }, s);

// ---------------------------------------------------------------- use cases
function rateBadge(rate) {
  if (rate === null || rate === undefined) return h("span", { class: "badge muted" }, "no baseline");
  const cls = rate >= 1 ? "ok" : rate >= 0.5 ? "warn" : "bad";
  return h("span", { class: `badge ${cls}` }, `baseline ${Math.round(rate * 100)}%`);
}

function renderCases() {
  const active = scenario ? scenario.id : session ? "" : undefined;
  const free = h("button", { class: `case ${active === "" ? "active" : ""}`, type: "button", onclick: () => start(null) },
    h("div", { class: "top" }, h("span", { class: "id" }, "💬"), h("span", { class: "title" }, "Free chat")),
    h("div", { class: "why" }, "No faults armed. Talk to the agent like a patient would."));
  const items = scenarios.map((s) => h("button", { class: `case ${active === s.id ? "active" : ""}`, type: "button", onclick: () => start(s.id) },
    h("div", { class: "top" }, h("span", { class: "id" }, s.id), h("span", { class: "title" }, s.title)),
    h("div", { class: "why" }, s.why),
    h("div", { class: "meta" }, h("span", { class: "badge" }, s.failure_class === "none" ? "happy path" : s.failure_class),
      rateBadge(s.baseline_pass_rate))));
  if (ChatAPI.isMock) items.push(h("div", { class: "why hint", style: "padding:8px 12px" }, "Use cases need LIVE mode (USE_MOCK = false in api.js)."));
  els.cases.replaceChildren(free, ...items);
}

async function loadHistory() {
  try {
    const items = await ChatAPI.history();
    els.history.replaceChildren(...(items.length ? items.map((it) => h("button", {
      class: `case hist ${session === it.session_id ? "active" : ""}`, type: "button", onclick: () => openPast(it.session_id) },
      h("div", { class: "top" }, h("span", { class: "id" }, it.scenario_id || "💬"), h("span", { class: "title" }, it.title),
        stateBadge(it.final_state)),
      h("div", { class: "why" }, `${it.messages} messages · ${it.last.replace("T", " ").slice(0, 16)} UTC${it.live ? " · live" : ""}`)))
      : [h("span", { class: "hint pad" }, "None yet.")]));
  } catch { /* history is optional */ }
}

async function openPast(sid) {
  if (busy) return;
  try {
    const st = await ChatAPI.open(sid);
    session = sid; readOnly = true;
    scenario = st.scenario_id ? scenarios.find((s) => s.id === st.scenario_id) : null;
    renderCases();
    els.title.textContent = `${scenario ? scenario.id + " · " + scenario.title : "Free chat"} (saved)`;
    els.sub.textContent = `session ${sid} · read from SQLite`;
    els.messages.replaceChildren(h("li", { class: "msg system" }, "Saved session — read-only replay from its SQLite file."));
    for (const m of st.transcript) {
      if (m.role === "user") addMessage("user", m.text, m.request_id ? [`request_id ${m.request_id}`] : []);
      else addMessage("agent", m.text, [stateBadge(m.state), m.trace_id ? h("span", { class: "mono" }, `trace ${m.trace_id.slice(0, 8)}…`) : null,
                                         m.error ? h("span", { class: "badge bad" }, "infra error") : null].filter(Boolean));
    }
    els.suggest.replaceChildren(); els.options.replaceChildren();
    renderStatus(st);
    setBusy(false);
    loadHistory();
  } catch (e) { addMessage("system", `Could not open: ${e.message}`); }
}

// ---------------------------------------------------------------- session
async function start(scenarioId) {
  if (busy) return;
  setBusy(true);
  scenario = scenarioId ? scenarios.find((s) => s.id === scenarioId) : null;
  session = "starting"; readOnly = false;
  renderCases();
  els.messages.replaceChildren(h("li", { class: "msg system" }, "Starting a fresh session…"));
  els.options.replaceChildren();
  els.checks.replaceChildren("Runs the same deterministic checks as the eval harness against this session.");
  try {
    const r = await ChatAPI.start(scenarioId);
    session = r.session_id;
    els.title.textContent = scenario ? `${scenario.id} · ${scenario.title}` : "Free chat";
    els.sub.textContent = `session ${session}` + (r.status.fake_clock ? " · simulated clock" : "");
    const notes = ["Session ready."];
    if (scenario?.faults?.length) notes.push("Faults armed (see right panel).");
    if (scenario?.setup?.prebook) notes.push("The patient already has a booking (setup).");
    els.messages.replaceChildren(h("li", { class: "msg system" }, notes.join(" ")));
    if (r.greeting) addAgent(r.greeting);
    renderSuggest();
    renderStatus(r.status);
    loadHistory();
  } catch (e) {
    session = null;
    els.messages.replaceChildren(h("li", { class: "msg system" }, `Could not start: ${e.message}`));
  }
  setBusy(false);
  els.input.focus();
}

function renderSuggest() {
  els.suggest.replaceChildren(...(scenario?.turns || []).map((t, i) => {
    const label = t.user.length > 70 ? t.user.slice(0, 68) + "…" : t.user;
    const chip = h("button", { class: "chip", type: "button", title: t.user },
      `${i + 1}. `,
      t.advance_minutes ? h("span", { class: "k" }, `⏩ +${t.advance_minutes}m then`) : null,
      t.request_id ? h("span", { class: "k" }, `[${t.request_id}]`) : null,
      label);
    chip.addEventListener("click", async () => {
      if (busy) return;
      chip.classList.add("used");
      if (t.advance_minutes) await advance(t.advance_minutes);
      send(t.user, t.request_id);
    });
    return chip;
  }));
}

// ---------------------------------------------------------------- chat
function addMessage(role, text, metaNodes = []) {
  const li = h("li", { class: `msg ${role}` }, text);
  if (metaNodes.length) li.append(h("span", { class: "meta" }, ...metaNodes));
  els.messages.append(li);
  els.messages.scrollTop = els.messages.scrollHeight;
  return li;
}

function addAgent(res) {
  const meta = [stateBadge(res.state)];
  if (res.latency_ms) meta.push(`${(res.latency_ms / 1000).toFixed(1)}s`);
  if (res.trace_url) meta.push(h("a", { href: res.trace_url, target: "_blank", rel: "noopener" }, "trace ↗"));
  if (res.error) meta.push(h("span", { class: "badge bad", title: res.error }, "infra error"));
  addMessage("agent", res.reply, meta);
  setOptions(res.options);
}

function setOptions(options = []) {
  els.options.replaceChildren(...options.map(({ label, value }) =>
    h("button", { type: "button", onclick: () => send(value, null, label) }, label)));
}

async function send(text, requestId, shown) {
  text = (text ?? els.input.value).trim();
  if (!text || !session || busy) return;
  els.input.value = "";
  addMessage("user", shown || text, requestId ? [`request_id ${requestId}`] : []);
  setOptions([]);
  const typing = addMessage("agent typing", "Agent is working…");
  setBusy(true);
  try {
    const res = await ChatAPI.send(text, requestId);
    typing.remove();
    addAgent(res);
    renderStatus(res.status);
    loadHistory();
  } catch (e) {
    typing.remove();
    addMessage("system", `Couldn't reach the scheduling service (${e.message}).`);
  }
  setBusy(false);
  els.input.focus();
}

async function advance(minutes) {
  try {
    const st = await ChatAPI.advance(minutes);
    addMessage("system", `⏩ Clock advanced ${minutes} minutes (now ${st.now.slice(11, 16)} UTC). Timeouts processed.`);
    renderStatus(st);
  } catch (e) { addMessage("system", e.message); }
}

// ---------------------------------------------------------------- status panel
function evText(e) {
  switch (e.name) {
    case "state.transition": return [`${e.from_state} → ${e.to_state}`, `${e.transition} · ${e.event}${e.cause ? " · " + e.cause : ""}`];
    case "tool.call": return [e.tool, `attempt ${e.attempt} · ${e.status ?? e.error} · ${e.outcome_class}`];
    case "failure.classified": return [`failure ${e.failure_class} on ${e.tool}`, e.detail ?? ""];
    case "retry.scheduled": return [`retry ${e.tool}`, `attempt ${e.attempt} after ${e.backoff_ms}ms`];
    case "reconcile.start": return ["reconcile: outcome unknown", `${e.op} · reading back by key`];
    case "reconcile.resolved": return [`reconcile → ${e.result}`, `${e.op} · ${e.reads} read(s)`];
    case "saga.step": return [`saga ${e.step} → ${e.status}`, e.attempt ? `attempt ${e.attempt}` : ""];
    case "escalation.packet": return ["escalated to staff", e.reason];
    case "circuit.open": return [`circuit open: ${e.tool}`, "stopped calling the failing service"];
    case "transition.rejected": return ["rejected (F9)", `${e.event} in ${e.state}: ${e.reason}`];
    default: return [e.name, Object.entries(e).filter(([k, v]) => !["name", "i", "trace_id", "appointment_id"].includes(k) && v != null)
      .map(([k, v]) => `${k}=${v}`).join(" · ")];
  }
}

function renderStatus(st) {
  const a = st.appointment;
  els.badge.textContent = a.state;
  els.badge.dataset.kind = kindOf(a.state);

  const idx = st.lifecycle.indexOf(a.state);
  const booked = a.state === "BOOKED";
  els.stepper.replaceChildren(...st.lifecycle.map((s, i) => h("div", {
    class: `step ${booked || i < idx ? "done" : i === idx ? "cur" : ""}`, title: s })));

  const rows = [
    ["patient", a.patient_id ? `${a.patient_id}${a.patient_verified ? " ✓ verified" : ""}` : "not verified"],
    ["provider", a.provider_id],
    ["slot", a.slot_start ? a.slot_start.replace("T", " ").slice(0, 16) + " UTC" : null],
    ["booking id", a.booking_id], ["notice", a.notice_status],
    ["hold expires", a.hold_expires_at ? a.hold_expires_at.slice(11, 16) + " UTC" : null],
    ["failed calls", a.failed_calls || null], ["ended because", a.terminal_reason],
    ["clock", st.now ? st.now.replace("T", " ").slice(0, 16) + " UTC" : null],
    ["source", st.read_only ? "SQLite (saved session)" : "SQLite (live session)"],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "");
  els.appt.replaceChildren(...rows.flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", { class: "v" }, String(v))]));

  const lf = st.langfuse;
  els.langfuse.replaceChildren(
    h("div", { class: "lf-row" }, h("span", { class: "k" }, "tracing"),
      h("span", { class: `badge ${lf.enabled ? "ok" : "muted"}` }, h("span", { class: "dot" }), lf.enabled ? "on" : "off (logs only)")),
    h("div", { class: "lf-row" }, h("span", { class: "k" }, "session"), h("span", { class: "mono" }, st.session_id || "—")),
    h("div", { class: "lf-row" }, h("span", { class: "k" }, "traces"), `${lf.trace_count} (one per message)`),
    lf.session_url ? h("div", { class: "lf-row" }, h("span", { class: "k" }, "open"),
      h("a", { href: lf.session_url, target: "_blank", rel: "noopener" }, "session ↗"), " · ",
      h("a", { href: lf.last_trace_url, target: "_blank", rel: "noopener" }, "last trace ↗")) : null,
  );

  els.faults.replaceChildren(...(st.faults.length ? st.faults.map((f) =>
    h("span", { class: "badge warn", title: `calls: ${JSON.stringify(f.calls)}` }, `${f.tool} · ${f.fault}`))
    : [h("span", { class: "hint" }, "None")]));

  els.bookings.replaceChildren(...(st.bookings.length ? st.bookings.map((b) => h("div", {},
    h("span", { class: `badge ${b.status === "ACTIVE" ? (b.kind === "BOOKING" ? "ok" : "") : "muted"}` }, `${b.kind} ${b.status}`), " ",
    h("span", { class: "mono" }, `${b.provider_id} ${(b.slot_start || "").slice(5, 16).replace("T", " ")}`), " ",
    h("span", { class: "hint" }, b.patient_id === "p_other" ? "(another patient)" : b.patient_id)))
    : [h("span", { class: "hint" }, "None")]));

  const evs = st.events.filter((e) => e.name !== "appointment.created");
  els.evCount.textContent = evs.length ? String(evs.length) : "";
  els.events.replaceChildren(...(evs.length ? evs.slice().reverse().map((e) => {
    const [n, x] = evText(e);
    const lvl = e.name === "tool.call" ? (e.outcome_class === "OK" ? "" : "warn") : (EV_LEVEL[e.name] || "");
    return h("li", { class: `ev ${lvl}` }, h("div", { class: "bar" }), h("div", {}, h("div", { class: "n" }, n), x ? h("div", { class: "x" }, x) : null));
  }) : [h("li", { class: "hint" }, "Events appear here as the agent works.")]));
}

async function runChecks() {
  try {
    const groups = await ChatAPI.checks();
    els.checks.replaceChildren(h("div", { class: "checks" }, ...Object.entries(groups).flatMap(([g, cs]) => [
      h("div", { class: "group-name" }, g),
      ...(cs.length ? cs.map((c) => h("div", { class: `check ${c.passed ? "pass" : "fail"}` },
        h("span", { class: "ico" }, c.passed ? "✓" : "✗"),
        h("span", {}, c.name, !c.passed && c.detail ? h("span", { class: "d" }, ` — ${c.detail}`) : null)))
        : [h("div", { class: "hint" }, "no expectations (free chat)")]),
    ])));
  } catch (e) { els.checks.replaceChildren(e.message); }
}

function setBusy(b) {
  busy = b;
  const live = session && session !== "starting" && !readOnly;
  els.send.disabled = b || !live;
  els.input.disabled = !live;
  els.reset.disabled = b || !session || session === "starting";
  els.checksBtn.disabled = b || !live;
  els.advance.disabled = b || !live || !scenario || ChatAPI.isMock;
  els.suggest.querySelectorAll(".chip").forEach((c) => (c.disabled = b));
}

// ---------------------------------------------------------------- boot
els.form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
els.reset.addEventListener("click", () => start(scenario ? scenario.id : null));
els.advance.addEventListener("click", () => advance(11));
els.checksBtn.addEventListener("click", runChecks);

(async () => {
  els.mode.textContent = ChatAPI.isMock ? "MOCK" : "LIVE";
  els.mode.classList.toggle("live", !ChatAPI.isMock);
  try {
    const [meta, scs] = await Promise.all([ChatAPI.meta(), ChatAPI.scenarios()]);
    scenarios = scs;
    els.model.textContent = meta.model;
    els.tracing.className = `badge ${meta.tracing_enabled ? "ok" : "muted"}`;
    els.tracing.replaceChildren(h("span", { class: "dot" }), `Langfuse ${meta.tracing_enabled ? "on · " + meta.environment : "off"}`);
    if (ChatAPI.isMock) document.getElementById("cost-note").textContent = "Mock mode: no Gemini or Langfuse calls";
  } catch (e) {
    els.model.textContent = "server unreachable";
  }
  renderCases();
  loadHistory();
})();
