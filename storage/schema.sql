-- Single SQLite file, two ownership zones.
-- Backend tables (system of record, owned by backend/): slots, patients, bookings, notifications.
-- Agent tables (lifecycle, owned by reliability/): appointments, intents, transitions,
-- request_dedupe, outbox, escalations.
-- The two zones only meet through HTTP (gateway -> backend). That separation is what
-- makes "the backend committed but the agent doesn't know" (F2) representable.

-- ---------- backend zone ----------
CREATE TABLE IF NOT EXISTS patients (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    dob         TEXT NOT NULL,
    phone       TEXT
);

CREATE TABLE IF NOT EXISTS slots (
    provider_id TEXT NOT NULL,
    visit_type  TEXT NOT NULL,
    slot_start  TEXT NOT NULL,
    slot_end    TEXT NOT NULL,
    PRIMARY KEY (provider_id, slot_start)
);

CREATE TABLE IF NOT EXISTS bookings (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('HOLD','BOOKING')),
    status          TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','EXPIRED','CANCELLED')),
    patient_id      TEXT NOT NULL,
    provider_id     TEXT NOT NULL,
    slot_start      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,   -- key of the creating call (hold); I2
    book_key        TEXT UNIQUE,            -- key of the promoting book call
    cancel_key      TEXT UNIQUE,            -- key of the cancel call
    expires_at      TEXT,                   -- holds only
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- I1: no two active holds/bookings on the same provider slot. Backstop for any agent bug.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_slot
    ON bookings(provider_id, slot_start) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY,
    booking_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

-- ---------- agent zone ----------
CREATE TABLE IF NOT EXISTS appointments (
    id               TEXT PRIMARY KEY,
    patient_id       TEXT,
    patient_verified INTEGER NOT NULL DEFAULT 0,          -- I8
    provider_id      TEXT,
    visit_type       TEXT,
    slot_start       TEXT,
    slot_end         TEXT,
    hold_id          TEXT,
    hold_expires_at  TEXT,
    booking_id       TEXT,
    state            TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 0,
    notice_status    TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    pending_json     TEXT,          -- scratch: offered slots, reschedule saga step, etc.
    failed_calls     INTEGER NOT NULL DEFAULT 0,          -- per-appointment budget (F6)
    last_activity_at TEXT NOT NULL,
    nudged           INTEGER NOT NULL DEFAULT 0,
    terminal_reason  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- I9: terminal states are immutable. The lifecycle fields are frozen; side-effect status
-- (notice_status) may still progress, e.g. the cancellation notice after CANCELLED.
CREATE TRIGGER IF NOT EXISTS trg_terminal_immutable
BEFORE UPDATE OF state, patient_id, provider_id, slot_start, booking_id ON appointments
WHEN OLD.state IN ('CANCELLED','EXPIRED','ESCALATED')
BEGIN
    SELECT RAISE(ABORT, 'I9: terminal appointment is immutable');
END;

CREATE TABLE IF NOT EXISTS intents (
    id              TEXT PRIMARY KEY,
    appointment_id  TEXT NOT NULL,
    op              TEXT NOT NULL,              -- hold | book | cancel | release | notify
    idempotency_key TEXT NOT NULL UNIQUE,       -- I2, I4
    args_json       TEXT NOT NULL,
    status          TEXT NOT NULL,              -- PENDING|SUCCESS|CONFLICT|REJECTED|UNKNOWN|RESOLVED_SUCCESS|RESOLVED_ABSENT
    attempts        INTEGER NOT NULL DEFAULT 0,
    result_json     TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id  TEXT NOT NULL,
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    event           TEXT NOT NULL,
    cause           TEXT,
    trace_id        TEXT,
    ts              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_dedupe (
    request_id      TEXT PRIMARY KEY,
    response_json   TEXT NOT NULL,
    ts              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id              TEXT PRIMARY KEY,
    appointment_id  TEXT NOT NULL,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,              -- PENDING|SENT|FAILED_RETRYING|FAILED_FINAL
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    id              TEXT PRIMARY KEY,
    appointment_id  TEXT NOT NULL,
    reason          TEXT NOT NULL,
    packet_json     TEXT NOT NULL,              -- I12
    created_at      TEXT NOT NULL
);

-- Persisted pipeline events (RELIABILITY_SPEC §7): every event any sink sees, so the UI and
-- post-mortems read history from SQLite instead of process memory.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    name            TEXT NOT NULL,
    appointment_id  TEXT,
    trace_id        TEXT,
    attrs_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_appt ON events(appointment_id);

-- Chat transcript, one row per message (agent zone).
CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user','agent')),
    text            TEXT NOT NULL,
    appointment_id  TEXT,
    state           TEXT,
    trace_id        TEXT,
    request_id      TEXT,
    error           TEXT,
    ts              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_session ON chat_messages(session_id);
