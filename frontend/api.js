// Chat API adapter. app.js only talks to ChatAPI; it never calls fetch directly.
//
// LIVE mode (USE_MOCK = false) talks to server/app.py:
//   GET  /api/meta                       model, tracing on/off, environment
//   GET  /api/scenarios                  use cases (eval/scenarios/*.yaml) + baseline pass rates
//   POST /api/sessions {scenario_id?}    fresh isolated session; arms the use case's faults
//   POST /api/sessions/:id/messages      {text, request_id?} -> {reply, state, trace_url, error, latency_ms, status}
//   GET  /api/sessions/:id/status        appointment, events, bookings, faults, langfuse
//   POST /api/sessions/:id/advance       {minutes} fast-forward the simulated clock (use cases only)
//   POST /api/sessions/:id/checks        deterministic eval checks against this session
//   GET  /api/history                    past sessions, read from their SQLite files (survives restarts)
//   GET  /api/history/:id                one past session: transcript + status, read-only
//
// MOCK mode (USE_MOCK = true) needs no backend and makes NO Gemini or Langfuse calls:
// a scripted happy path plus cancel / decline / emergency, free chat only.

const USE_MOCK = false;
const API_BASE = "";   // same origin as server/app.py; e.g. "http://localhost:8000" if served elsewhere

const ChatAPI = (() => {
  async function call(path, body) {
    const res = await fetch(API_BASE + path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    return res.json();
  }
  let sessionId = null;

  const live = {
    meta: () => call("/api/meta"),
    scenarios: () => call("/api/scenarios"),
    async start(scenarioId) {
      const r = await call("/api/sessions", { scenario_id: scenarioId });
      sessionId = r.session_id;
      return r;
    },
    send: (text, requestId) => call(`/api/sessions/${sessionId}/messages`, { text, request_id: requestId || null }),
    status: () => call(`/api/sessions/${sessionId}/status`),
    advance: (minutes) => call(`/api/sessions/${sessionId}/advance`, { minutes }),
    checks: () => call(`/api/sessions/${sessionId}/checks`, {}),
    history: () => call("/api/history"),
    open: (sid) => call(`/api/history/${encodeURIComponent(sid)}`),
  };

  // ---- Mock agent: scripted, no backend needed ----
  const SLOTS = [
    { label: "Wed 7 Oct, 09:00 · Dr Rao", value: "2026-10-07T09:00:00+00:00", provider: "dr_rao" },
    { label: "Wed 7 Oct, 09:30 · Dr Rao", value: "2026-10-07T09:30:00+00:00", provider: "dr_rao" },
    { label: "Wed 7 Oct, 10:00 · Dr Mehta", value: "2026-10-07T10:00:00+00:00", provider: "dr_mehta" },
  ];
  let mock;
  function resetMock() {
    mock = { state: "COLLECTING", step: "identity", slot: null, events: [], verified: false, notice: "NOT_REQUIRED" };
  }
  resetMock();

  function mockStatus() {
    return {
      session_id: sessionId, scenario_id: null, now: new Date().toISOString(), fake_clock: false,
      appointment: {
        state: mock.state, patient_id: mock.verified ? "p_001" : null, patient_verified: mock.verified,
        provider_id: mock.slot?.provider ?? null, slot_start: mock.slot?.value ?? null,
        booking_id: mock.state === "BOOKED" ? "bk_mock" : null, notice_status: mock.notice,
      },
      lifecycle: ["COLLECTING", "SEARCHING", "OFFERED", "HELD", "CONFIRMING", "COMMITTING", "BOOKED"],
      events: mock.events, bookings: [], escalations: [], faults: [], tool_calls: {},
      langfuse: { enabled: false, last_trace_url: null, session_url: null, trace_count: 0 },
    };
  }
  // `path` is a list of [transitionId, toState]; the mock records them as state.transition events.
  function r(reply, path = [], options = []) {
    for (const [id, to] of path) {
      mock.events.push({ i: mock.events.length, name: "state.transition", from_state: mock.state, to_state: to, transition: id, event: "mock" });
      mock.state = to;
    }
    return { reply, state: mock.state, options, trace_url: null, error: null, latency_ms: 0, status: mockStatus() };
  }
  const yesNo = [{ label: "Yes, book it", value: "yes" }, { label: "No", value: "no" }];

  function mockReply(text) {
    const t = text.trim().toLowerCase();
    if (/\b(chest pain|emergency|can't breathe)\b/.test(t) && !["CANCELLED", "EXPIRED", "ESCALATED"].includes(mock.state))
      return r("This sounds urgent. Please call emergency services now. I've passed this conversation to a member of staff.", [["T26", "ESCALATED"]]);
    if (t === "cancel" && mock.state === "BOOKED")
      return r("Your appointment is cancelled.", [["T22", "CANCELLING"], ["T23", "CANCELLED"]]);
    if (["CANCELLED", "EXPIRED", "ESCALATED"].includes(mock.state))
      return r("This conversation has ended. Start a new conversation to book again.");
    switch (mock.step) {
      case "identity":
        mock.step = "visit";
        return r("To verify you, please give your full name and date of birth (e.g. Asha Verma, 1990-04-12).");
      case "visit":
        mock.step = "choose"; mock.verified = true;
        return r("Verified. Here are the nearest available check-up slots:", [["T01", "SEARCHING"], ["T02", "OFFERED"]], SLOTS);
      case "choose": {
        const slot = SLOTS.find((s) => s.value === text || s.label.toLowerCase() === t);
        if (!slot) return r("Please pick one of the slots below.", [], SLOTS);
        mock.slot = slot; mock.step = "confirm";
        return r(`I'm holding ${slot.label} for 10 minutes. Shall I book it?`, [["T04", "HELD"], ["T06", "CONFIRMING"]], yesNo);
      }
      case "confirm":
        if (t === "yes" || t === "yes, book it") {
          mock.step = "done"; mock.notice = "SENT";
          return r(`You're booked for ${mock.slot.label}. A confirmation message has been sent.`,
            [["T08", "COMMITTING"], ["T11", "BOOKED"]], [{ label: "Cancel appointment", value: "cancel" }]);
        }
        if (t === "no") {
          mock.step = "choose";
          return r("No problem, I've released that hold. Pick another slot:", [["T10", "OFFERED"]], SLOTS);
        }
        return r("I need a clear yes or no before booking.", [], yesNo);
      default:
        return r("You're all set. Anything else? You can say \"cancel\".");
    }
  }

  const mockApi = {
    meta: async () => ({ model: "mock (no LLM)", tracing_enabled: false, environment: "mock" }),
    scenarios: async () => [],   // use cases need the real backend + fault injector
    async start() {
      sessionId = `mock-${crypto.randomUUID().slice(0, 8)}`; resetMock();
      return { session_id: sessionId, status: mockStatus(),
               greeting: r("Hi! I can help you book, reschedule or cancel an appointment. What would you like to do?",
                           [], [{ label: "Book an appointment", value: "I'd like to book an appointment" }]) };
    },
    async send(text) {
      await new Promise((res) => setTimeout(res, 300 + Math.random() * 400));   // fake latency
      return mockReply(text);
    },
    status: async () => mockStatus(),
    advance: async () => { throw new Error("time travel needs LIVE mode"); },
    checks: async () => ({ "mock-mode": [{ name: "checks need LIVE mode", passed: false, detail: "set USE_MOCK = false" }] }),
    history: async () => [],
    open: async () => { throw new Error("history needs LIVE mode"); },
  };

  return { isMock: USE_MOCK, ...(USE_MOCK ? mockApi : live) };
})();
