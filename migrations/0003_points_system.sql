-- 0003_points_system: 小テスト 5 問構成・配点ベース集計 (SPEC_tutor.md §3.5)
--
-- 既存の learning_records.score / learning_records.total は正解数 / 問題数として
-- 後方互換のため残し、配点 (10/15/5) ベースの集計は専用列に分離する。
--
-- SQLite の ALTER TABLE ADD COLUMN は IF NOT EXISTS 非対応のため
-- schema_migrations による再適用防止に依存する (既存 0001/0002 と同方針)。

ALTER TABLE learning_records ADD COLUMN points_total   INTEGER DEFAULT 0;
ALTER TABLE learning_records ADD COLUMN points_earned  INTEGER DEFAULT 0;

ALTER TABLE question_attempts ADD COLUMN tier          TEXT;
ALTER TABLE question_attempts ADD COLUMN points        INTEGER DEFAULT 0;
ALTER TABLE question_attempts ADD COLUMN earned_points INTEGER DEFAULT 0;
