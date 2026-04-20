-- 0002_report_cache: 日次レポートのキャッシュ (C-6)
-- 同日・同入力で保護者が再送しても Claude を再呼び出ししないため、
-- プロンプト全体の sha256 をキーに body を保持する。

CREATE TABLE IF NOT EXISTS daily_reports (
    user_id      TEXT NOT NULL,
    day          DATE NOT NULL,
    records_hash TEXT NOT NULL,
    body         TEXT NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, day, records_hash)
);

CREATE INDEX IF NOT EXISTS ix_daily_reports_user_day ON daily_reports(user_id, day);
