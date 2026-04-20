-- 0001_init: 現行スキーマのベースライン
-- すべて IF NOT EXISTS なので、既存の本番 DB に対して流しても no-op。
-- schema_migrations に 0001 を記録することで以後 skip される。

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    status     TEXT DEFAULT 'collecting'
);

CREATE TABLE IF NOT EXISTS session_images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    image_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    subject          TEXT NOT NULL,
    unit             TEXT NOT NULL,
    concept_keys     TEXT,
    grade_introduced INTEGER,
    first_seen_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    mastery          REAL DEFAULT 0.0,
    UNIQUE(subject, unit)
);

CREATE TABLE IF NOT EXISTS learning_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL,
    topic_id            INTEGER NOT NULL,
    user_id             TEXT NOT NULL,
    source_summary      TEXT,
    detected_difficulty TEXT,
    stumble_points      TEXT,
    questions           TEXT,
    total               INTEGER,
    score               INTEGER,
    status              TEXT DEFAULT 'waiting',
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS question_attempts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    learning_record_id INTEGER NOT NULL,
    topic_id           INTEGER NOT NULL,
    question_text      TEXT NOT NULL,
    correct_answer     TEXT,
    student_answer     TEXT,
    is_correct         INTEGER NOT NULL,
    question_type      TEXT,
    concept_keys       TEXT,
    mistake_category   TEXT,
    teaching_note      TEXT,
    intent             TEXT,
    origin             TEXT DEFAULT 'today',
    attempted_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL,
    topic_id       INTEGER NOT NULL,
    concept_key    TEXT,
    reason         TEXT NOT NULL,
    scheduled_for  DATE NOT NULL,
    interval_days  INTEGER NOT NULL DEFAULT 1,
    times_reviewed INTEGER NOT NULL DEFAULT 0,
    last_result    TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS credits (
    user_id    TEXT PRIMARY KEY,
    balance    INTEGER DEFAULT 0 CHECK(balance >= 0),
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS exchanges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    prize_name TEXT NOT NULL,
    cost       INTEGER NOT NULL,
    status     TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id     TEXT PRIMARY KEY,
    processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS supervisor_state (
    user_id    TEXT PRIMARY KEY,
    mode       TEXT NOT NULL DEFAULT 'parent' CHECK(mode IN ('parent','child')),
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_sessions_user_status           ON sessions(user_id, status, id DESC);
CREATE INDEX IF NOT EXISTS ix_session_images_session         ON session_images(session_id, id);
CREATE INDEX IF NOT EXISTS ix_learning_records_session       ON learning_records(session_id, status);
CREATE INDEX IF NOT EXISTS ix_learning_records_user          ON learning_records(user_id, status, created_at);
CREATE INDEX IF NOT EXISTS ix_question_attempts_lr           ON question_attempts(learning_record_id);
CREATE INDEX IF NOT EXISTS ix_question_attempts_topic        ON question_attempts(topic_id, is_correct, attempted_at);
CREATE INDEX IF NOT EXISTS ix_review_queue_user              ON review_queue(user_id, status, scheduled_for);
CREATE INDEX IF NOT EXISTS ix_exchanges_user_status          ON exchanges(user_id, status, id);
