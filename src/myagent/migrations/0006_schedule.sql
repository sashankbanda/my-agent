-- 0006: scheduled and recurring tasks.
--
-- One row per standing instruction ("every weekday at 8am, brief me").
-- ``task`` is the natural-language request handed to the agent loop, exactly
-- as if it had been typed - so a schedule is not a separate execution path
-- with its own bugs, just a turn with a clock for a trigger.
--
-- ``next_run`` is stored, not derived: the poller's query is a single indexed
-- comparison rather than parsing every cron expression every tick, and a
-- restart resumes from the persisted value instead of re-deriving it.
--
-- ``last_run`` is what makes a misfire safe. When the machine was asleep at
-- 8am, the poller must run the job once on waking, not once per missed
-- interval - it compares against last_run and skips forward.

CREATE TABLE schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cron TEXT NOT NULL,              -- 5-field cron, evaluated in local time
    task TEXT NOT NULL,              -- the request to run, in plain language
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    next_run TEXT NOT NULL,          -- ISO-8601 local time; the poller's index
    last_run TEXT,                   -- NULL until it has fired once
    last_status TEXT,                -- 'ok' | 'failed' | 'skipped'
    last_error TEXT,                 -- honest record of why a run failed
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- The poller's only hot query: "what is due?"
CREATE INDEX idx_schedules_due ON schedules (enabled, next_run);
