import base64
import hashlib
import hmac
import json
import os
import sqlite3
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import anthropic
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, abort, request

load_dotenv()

app = Flask(__name__)

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PARENT_USER_ID = os.environ["PARENT_USER_ID"]
CHILD_USER_ID = os.environ["CHILD_USER_ID"]
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "21"))

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_CONTENT_URL = "https://api-data.line.me/v2/bot/message/{}/content"
DB_PATH = os.path.join(os.path.dirname(__file__), "study_bot.db")
IMAGE_DIR = os.path.join(os.path.dirname(__file__), "images")
PRIZES_PATH = os.path.join(os.path.dirname(__file__), "prizes.json")
PROFILE_PATH = os.path.join(os.path.dirname(__file__), "student_profile.json")
CREDIT_PER_CORRECT = 10

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
os.makedirs(IMAGE_DIR, exist_ok=True)


# ── DB ──────────────────────────────────────────────────────────────────────

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                status     TEXT DEFAULT 'collecting'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_images (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                image_path TEXT NOT NULL
            )
        """)
        conn.execute("""
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
            )
        """)
        conn.execute("""
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
            )
        """)
        conn.execute("""
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
            )
        """)
        conn.execute("""
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
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS credits (
                user_id    TEXT PRIMARY KEY,
                balance    INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exchanges (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                prize_name TEXT NOT NULL,
                cost       INTEGER NOT NULL,
                status     TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webhook_events (
                event_id     TEXT PRIMARY KEY,
                processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


# ── DB helpers ───────────────────────────────────────────────────────────────

def get_active_session(user_id: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sessions WHERE user_id=? AND status IN ('collecting','grading') ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None

def create_session(user_id: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("INSERT INTO sessions (user_id) VALUES (?)", (user_id,)).lastrowid

def add_session_image(session_id: int, image_path: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO session_images (session_id, image_path) VALUES (?,?)", (session_id, image_path))

def get_session_images(session_id: int) -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT image_path FROM session_images WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()]

def get_or_create_topic(subject: str, unit: str | None = None, concept_keys: list | None = None,
                        grade: int | None = None) -> int:
    unit = unit or subject  # P2段階ではunit未検出のためsubjectで代用
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM topics WHERE subject=? AND unit=?", (subject, unit)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE topics SET last_seen_at = CURRENT_TIMESTAMP WHERE id=?", (row[0],)
            )
            return row[0]
        return conn.execute(
            "INSERT INTO topics (subject, unit, concept_keys, grade_introduced) VALUES (?,?,?,?)",
            (subject, unit, json.dumps(concept_keys or [], ensure_ascii=False), grade),
        ).lastrowid

def save_learning_records(session_id: int, user_id: str, subjects_data: list) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        for s in subjects_data:
            topic_id = get_or_create_topic(
                s["subject_name"],
                unit=s.get("unit_guess"),
                concept_keys=s.get("concept_keys"),
            )
            conn.execute(
                """INSERT INTO learning_records
                   (session_id, topic_id, user_id, source_summary,
                    detected_difficulty, stumble_points, questions, total)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (session_id, topic_id, user_id, s["summary"],
                 s.get("difficulty"),
                 json.dumps(s.get("stumble_points") or [], ensure_ascii=False),
                 json.dumps(s["questions"], ensure_ascii=False),
                 len(s["questions"])),
            )

def get_next_unanswered(session_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT lr.id AS id, lr.topic_id, lr.total, lr.questions,
                      lr.source_summary, t.subject AS subject_name, t.unit
               FROM learning_records lr
               JOIN topics t ON lr.topic_id = t.id
               WHERE lr.session_id=? AND lr.status='waiting' ORDER BY lr.id LIMIT 1""",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(row) | {"questions": json.loads(row["questions"])}

def complete_learning_record(record_id: int, score: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE learning_records SET score=?, status='done' WHERE id=?",
            (score, record_id),
        )

def count_waiting_records(session_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM learning_records WHERE session_id=? AND status='waiting'",
            (session_id,),
        ).fetchone()[0]

def save_question_attempts(learning_record_id: int, topic_id: int,
                           questions: list, results: list) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        for q, r in zip(questions, results):
            conn.execute(
                """INSERT INTO question_attempts
                   (learning_record_id, topic_id, question_text, correct_answer,
                    student_answer, is_correct, question_type, concept_keys,
                    mistake_category, teaching_note, intent, origin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (learning_record_id, topic_id, q.get("q", ""), q.get("a"),
                 r.get("student_answer"),
                 1 if r.get("correct") else 0,
                 q.get("type") or r.get("question_type"),
                 json.dumps(q.get("concept_keys") or r.get("concept_keys") or [], ensure_ascii=False),
                 r.get("mistake_category"),
                 r.get("teaching_note"),
                 q.get("intent"),
                 q.get("origin", "today")),
            )

REVIEW_INTERVALS = [1, 3, 7, 14, 30]  # 忘却曲線：正答で次の段階へ、30日超えで卒業
MASTERY_ALPHA = 0.4                    # 指数移動平均の重み（新しい結果）

def update_review_queue_from_result(user_id: str, topic_id: int,
                                    questions: list, results: list) -> None:
    """1セッション分の採点結果から review_queue を更新する。
    - 誤答: interval=1 で pending エントリを作成または更新（既存は1にリセット）
    - 正答: 既存の pending エントリを次の間隔に昇格、30日超えなら retired
    - concept_key は questions[i].concept_keys の先頭を使用（無ければ NULL）"""
    today = date.today()
    with sqlite3.connect(DB_PATH) as conn:
        for q, r in zip(questions, results):
            concept_keys = q.get("concept_keys") or r.get("concept_keys") or []
            concept_key = concept_keys[0] if concept_keys else None
            is_correct = bool(r.get("correct"))
            existing = conn.execute(
                """SELECT id, interval_days, times_reviewed FROM review_queue
                   WHERE user_id=? AND topic_id=?
                     AND COALESCE(concept_key,'')=COALESCE(?,'')
                     AND status='pending'
                   ORDER BY id DESC LIMIT 1""",
                (user_id, topic_id, concept_key),
            ).fetchone()

            if is_correct:
                if existing is None:
                    continue  # 正答で新規エントリは作らない
                rid, interval, times = existing
                # 次の間隔を決定
                if interval in REVIEW_INTERVALS:
                    idx = REVIEW_INTERVALS.index(interval)
                else:
                    idx = 0
                next_idx = idx + 1
                if next_idx >= len(REVIEW_INTERVALS):
                    # 30日を超えた → 卒業
                    conn.execute(
                        """UPDATE review_queue SET status='retired',
                           last_result='correct', times_reviewed=?,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (times + 1, rid),
                    )
                else:
                    next_interval = REVIEW_INTERVALS[next_idx]
                    next_date = (today + timedelta(days=next_interval)).isoformat()
                    conn.execute(
                        """UPDATE review_queue SET interval_days=?, scheduled_for=?,
                           times_reviewed=?, last_result='correct',
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (next_interval, next_date, times + 1, rid),
                    )
            else:
                # 誤答 → 1日後に再出題
                next_date = (today + timedelta(days=1)).isoformat()
                if existing:
                    rid, _, times = existing
                    conn.execute(
                        """UPDATE review_queue SET interval_days=1, scheduled_for=?,
                           times_reviewed=?, last_result='incorrect',
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (next_date, times + 1, rid),
                    )
                else:
                    conn.execute(
                        """INSERT INTO review_queue
                           (user_id, topic_id, concept_key, reason, scheduled_for,
                            interval_days, times_reviewed, last_result, status)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (user_id, topic_id, concept_key, 'mistake', next_date,
                         1, 0, 'incorrect', 'pending'),
                    )


def update_topic_mastery(topic_id: int, score: int, total: int,
                         alpha: float = MASTERY_ALPHA) -> float:
    """指数移動平均で topics.mastery を更新し、新しい mastery を返す。
    mastery_new = α * (score/total) + (1-α) * mastery_old"""
    if total <= 0:
        return 0.0
    ratio = score / total
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT mastery FROM topics WHERE id=?", (topic_id,)).fetchone()
        old = row[0] if row and row[0] is not None else 0.0
        new = alpha * ratio + (1 - alpha) * old
        conn.execute("UPDATE topics SET mastery=?, last_seen_at=CURRENT_TIMESTAMP WHERE id=?",
                     (new, topic_id))
    return new


def get_due_reviews(user_id: str, today_iso: str | None = None,
                    limit: int = 5) -> list[dict]:
    """本日以前に scheduled_for が到達した pending エントリを古い順に返す。"""
    today_iso = today_iso or date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT rq.*, t.subject, t.unit FROM review_queue rq
               JOIN topics t ON rq.topic_id = t.id
               WHERE rq.user_id=? AND rq.status='pending' AND rq.scheduled_for<=?
               ORDER BY rq.scheduled_for ASC, rq.id ASC LIMIT ?""",
            (user_id, today_iso, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def format_due_reviews_brief(due_reviews: list[dict]) -> str:
    """ステップA向け: 復習候補の簡易一覧。"""
    if not due_reviews:
        return ""
    lines = []
    for r in due_reviews:
        lines.append(
            f"- {r['subject']}「{r['unit']}」 概念={r['concept_key'] or '(全体)'}"
            f" 予定日={r['scheduled_for']}"
        )
    return "\n".join(lines)


def format_due_reviews_for_prompt(due_reviews: list[dict]) -> str:
    """ステップB向け: 復習候補を問題作成に使える粒度で整形。
    各候補に直近の誤答（question_text/student_answer/mistake_category）を添える。"""
    if not due_reviews:
        return ""
    lines = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for i, r in enumerate(due_reviews, 1):
            lines.append(
                f"[{i}] review_topic_id={r['topic_id']} 科目={r['subject']}"
                f" 単元=「{r['unit']}」 概念={r['concept_key'] or '(全体)'}"
                f" 理由={r['reason']} 予定日={r['scheduled_for']}"
            )
            if r.get("concept_key"):
                attempt = conn.execute(
                    """SELECT question_text, student_answer, mistake_category
                       FROM question_attempts
                       WHERE topic_id=? AND is_correct=0 AND concept_keys LIKE ?
                       ORDER BY id DESC LIMIT 1""",
                    (r["topic_id"], f'%"{r["concept_key"]}"%'),
                ).fetchone()
            else:
                attempt = conn.execute(
                    """SELECT question_text, student_answer, mistake_category
                       FROM question_attempts
                       WHERE topic_id=? AND is_correct=0
                       ORDER BY id DESC LIMIT 1""",
                    (r["topic_id"],),
                ).fetchone()
            if attempt:
                lines.append(
                    f"    前回誤答例: Q. {attempt['question_text']}"
                    f" / 生徒の答え: {attempt['student_answer'] or '(無記入)'}"
                    f" / 原因分類: {attempt['mistake_category'] or '不明'}"
                )
    return "\n".join(lines)


def apply_grading_results(user_id: str, default_topic_id: int,
                          learning_record_id: int,
                          questions: list, results: list) -> dict:
    """採点結果を question_attempts / review_queue / topics.mastery に反映する。
    復習問題（origin='review' かつ review_topic_id あり）は復習対象トピックに紐づける。
    戻り値: トピック別の (score, total) 集計と更新後 mastery。"""
    q_topics = []
    for q in questions:
        if q.get("origin") == "review" and q.get("review_topic_id"):
            q_topics.append(int(q["review_topic_id"]))
        else:
            q_topics.append(default_topic_id)

    # 1. question_attempts 保存（各問ごとのトピックで）
    with sqlite3.connect(DB_PATH) as conn:
        for q, r, tid in zip(questions, results, q_topics):
            conn.execute(
                """INSERT INTO question_attempts
                   (learning_record_id, topic_id, question_text, correct_answer,
                    student_answer, is_correct, question_type, concept_keys,
                    mistake_category, teaching_note, intent, origin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (learning_record_id, tid, q.get("q", ""), q.get("a"),
                 r.get("student_answer"),
                 1 if r.get("correct") else 0,
                 q.get("type") or r.get("question_type"),
                 json.dumps(q.get("concept_keys") or r.get("concept_keys") or [], ensure_ascii=False),
                 r.get("mistake_category"),
                 r.get("teaching_note"),
                 q.get("intent"),
                 q.get("origin", "today")),
            )

    # 2. review_queue 更新（トピック別にグループ化）
    by_topic: dict[int, tuple[list, list]] = {}
    for q, r, tid in zip(questions, results, q_topics):
        by_topic.setdefault(tid, ([], []))
        by_topic[tid][0].append(q)
        by_topic[tid][1].append(r)
    for tid, (qs, rs) in by_topic.items():
        update_review_queue_from_result(user_id, tid, qs, rs)

    # 3. mastery 更新（トピック別の正答率で）
    summary: dict[int, dict] = {}
    for r, tid in zip(results, q_topics):
        agg = summary.setdefault(tid, {"score": 0, "total": 0})
        agg["total"] += 1
        if r.get("correct"):
            agg["score"] += 1
    for tid, agg in summary.items():
        agg["mastery"] = update_topic_mastery(tid, agg["score"], agg["total"])

    return summary


def set_session_status(session_id: int, status: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE sessions SET status=? WHERE id=?", (status, session_id))

def get_credits(user_id: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT balance FROM credits WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else 0

def add_credits(user_id: str, amount: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO credits (user_id, balance) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               balance = balance + excluded.balance,
               updated_at = CURRENT_TIMESTAMP""",
            (user_id, amount),
        )
        return conn.execute("SELECT balance FROM credits WHERE user_id=?", (user_id,)).fetchone()[0]

def deduct_credits(user_id: str, amount: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """UPDATE credits SET balance = balance - ?, updated_at = CURRENT_TIMESTAMP
               WHERE user_id = ?""",
            (amount, user_id),
        )
        return conn.execute("SELECT balance FROM credits WHERE user_id=?", (user_id,)).fetchone()[0]

def get_daily_summary(user_id: str, target_date: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT t.subject AS subject_name, t.unit,
                      lr.score, lr.total, lr.source_summary AS summary
               FROM learning_records lr
               JOIN sessions ss ON lr.session_id = ss.id
               JOIN topics t ON lr.topic_id = t.id
               WHERE lr.user_id=? AND DATE(ss.created_at,'localtime')=? AND lr.status='done'""",
            (user_id, target_date),
        ).fetchall()
    return [dict(r) for r in rows]

def get_weekly_summary(user_id: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT DATE(ss.created_at,'localtime') as day, t.subject AS subject_name, t.unit,
                      lr.score, lr.total, lr.source_summary AS summary
               FROM learning_records lr
               JOIN sessions ss ON lr.session_id = ss.id
               JOIN topics t ON lr.topic_id = t.id
               WHERE lr.user_id=? AND DATE(ss.created_at,'localtime') >= DATE('now','localtime','-6 days') AND lr.status='done'
               ORDER BY ss.created_at""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]

def get_recent_topics_summary(user_id: str, days: int = 30) -> str:
    """直近days日の学習トピックと正答率・定着度をサマリ文字列で返す（プロンプト注入用）。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT DATE(ss.created_at,'localtime') as day, t.subject, t.unit,
                       lr.score, lr.total, t.mastery
                FROM learning_records lr
                JOIN sessions ss ON lr.session_id = ss.id
                JOIN topics t ON lr.topic_id = t.id
                WHERE lr.user_id=? AND DATE(ss.created_at,'localtime') >= DATE('now','localtime','-{int(days)} days')
                  AND lr.status='done'
                ORDER BY ss.created_at DESC LIMIT 30""",
            (user_id,),
        ).fetchall()
    if not rows:
        return ""
    lines = []
    for r in rows:
        ratio = (r["score"] / r["total"]) if r["total"] else 0
        lines.append(
            f"- {r['day']} {r['subject']}「{r['unit']}」"
            f"正答率{ratio:.0%} (定着度{r['mastery']:.2f})"
        )
    return "\n".join(lines)


def get_weak_points(user_id: str, days: int = 7, limit: int = 3) -> list[dict]:
    """直近days日の誤答を (科目, 単元, 概念キー) で集計し件数順に返す。
    各エントリに最新の誤答例（teaching_note含む）と mistake_category 分布を添える。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT qa.question_text, qa.student_answer, qa.correct_answer,
                       qa.mistake_category, qa.teaching_note, qa.concept_keys,
                       qa.attempted_at, t.subject, t.unit
                FROM question_attempts qa
                JOIN learning_records lr ON qa.learning_record_id = lr.id
                JOIN topics t ON qa.topic_id = t.id
                WHERE lr.user_id=? AND qa.is_correct=0
                  AND DATE(qa.attempted_at,'localtime') >= DATE('now','localtime','-{int(days)} days')
                ORDER BY qa.attempted_at DESC""",
            (user_id,),
        ).fetchall()
    groups: dict[tuple, dict] = {}
    for r in rows:
        try:
            keys = json.loads(r["concept_keys"] or "[]")
        except json.JSONDecodeError:
            keys = []
        ck = keys[0] if keys else ""
        gk = (r["subject"], r["unit"], ck)
        g = groups.setdefault(gk, {
            "subject": r["subject"], "unit": r["unit"], "concept_key": ck,
            "count": 0, "categories": {}, "example": None,
        })
        g["count"] += 1
        cat = r["mistake_category"] or "unknown"
        g["categories"][cat] = g["categories"].get(cat, 0) + 1
        if g["example"] is None:
            g["example"] = {
                "question_text": r["question_text"],
                "student_answer": r["student_answer"],
                "correct_answer": r["correct_answer"],
                "teaching_note": r["teaching_note"],
                "mistake_category": r["mistake_category"],
            }
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:limit]


def get_mastery_trend(user_id: str, days: int = 7) -> list[dict]:
    """直近days日と直前days日で、トピックごとの正答率を比較。
    recent_ratio / prior_ratio / delta を付与。attempts が無い側は None。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT qa.topic_id, t.subject, t.unit, t.mastery AS current_mastery,
                       qa.is_correct, DATE(qa.attempted_at,'localtime') AS day
                FROM question_attempts qa
                JOIN learning_records lr ON qa.learning_record_id = lr.id
                JOIN topics t ON qa.topic_id = t.id
                WHERE lr.user_id=?
                  AND DATE(qa.attempted_at,'localtime') >= DATE('now','localtime','-{int(days)*2} days')""",
            (user_id,),
        ).fetchall()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    topics: dict[int, dict] = {}
    for r in rows:
        t = topics.setdefault(r["topic_id"], {
            "topic_id": r["topic_id"], "subject": r["subject"], "unit": r["unit"],
            "current_mastery": r["current_mastery"],
            "recent_score": 0, "recent_total": 0,
            "prior_score": 0, "prior_total": 0,
        })
        if r["day"] > cutoff:
            t["recent_total"] += 1
            if r["is_correct"]:
                t["recent_score"] += 1
        else:
            t["prior_total"] += 1
            if r["is_correct"]:
                t["prior_score"] += 1
    for t in topics.values():
        t["recent_ratio"] = (t["recent_score"] / t["recent_total"]) if t["recent_total"] else None
        t["prior_ratio"] = (t["prior_score"] / t["prior_total"]) if t["prior_total"] else None
        t["delta"] = (t["recent_ratio"] - t["prior_ratio"]) if (
            t["recent_ratio"] is not None and t["prior_ratio"] is not None) else None
    return sorted(topics.values(), key=lambda x: x["current_mastery"] or 0)


def get_recent_teaching_notes(user_id: str, days: int = 7, limit: int = 10) -> list[dict]:
    """直近days日の teaching_note を新しい順に（誤答優先で）取得。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT qa.question_text, qa.student_answer, qa.is_correct,
                       qa.mistake_category, qa.teaching_note,
                       t.subject, t.unit
                FROM question_attempts qa
                JOIN learning_records lr ON qa.learning_record_id = lr.id
                JOIN topics t ON qa.topic_id = t.id
                WHERE lr.user_id=? AND qa.teaching_note IS NOT NULL AND qa.teaching_note<>''
                  AND DATE(qa.attempted_at,'localtime') >= DATE('now','localtime','-{int(days)} days')
                ORDER BY qa.is_correct ASC, qa.attempted_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def format_weak_points_block(weak: list[dict]) -> str:
    if not weak:
        return "（直近の誤答傾向は特になし）"
    lines = []
    for w in weak:
        cats = "、".join(f"{k}×{v}" for k, v in w["categories"].items())
        ck = f"[{w['concept_key']}]" if w["concept_key"] else ""
        lines.append(f"- {w['subject']}「{w['unit']}」{ck} 誤答{w['count']}回 ({cats})")
        ex = w.get("example") or {}
        if ex.get("question_text"):
            lines.append(
                f"    例) Q: {ex['question_text']}"
                f" / 生徒の答え: {ex.get('student_answer') or '(無記入)'}"
                f" / 正解: {ex.get('correct_answer') or '-'}"
            )
        if ex.get("teaching_note"):
            lines.append(f"    所見: {ex['teaching_note']}")
    return "\n".join(lines)


def format_mastery_trend_block(trend: list[dict]) -> str:
    if not trend:
        return "（推移データなし）"
    lines = []
    for t in trend:
        cur = t.get("current_mastery") or 0
        if t["delta"] is None:
            delta_str = "(初出)" if t["prior_total"] == 0 and t["recent_total"] else ""
        else:
            arrow = "↑" if t["delta"] > 0.05 else "↓" if t["delta"] < -0.05 else "→"
            delta_str = f"{arrow}{t['delta']:+.0%}"
        recent = (f"{t['recent_ratio']:.0%}" if t['recent_ratio'] is not None else "-")
        prior = (f"{t['prior_ratio']:.0%}" if t['prior_ratio'] is not None else "-")
        lines.append(
            f"- {t['subject']}「{t['unit']}」定着度{cur:.2f}"
            f" 今週{recent}/前週{prior} {delta_str}"
        )
    return "\n".join(lines)


def create_exchange(user_id: str, prize_name: str, cost: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "INSERT INTO exchanges (user_id, prize_name, cost) VALUES (?,?,?)",
            (user_id, prize_name, cost),
        ).lastrowid

def get_pending_exchanges() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM exchanges WHERE status='pending' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]

def update_exchange_status(exchange_id: int, status: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE exchanges SET status=? WHERE id=?", (status, exchange_id))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM exchanges WHERE id=?", (exchange_id,)).fetchone()
    return dict(row) if row else None


# ── LINE API ─────────────────────────────────────────────────────────────────

LINE_TIMEOUT = (5, 30)          # (connect, read) 秒
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # LINE画像の受入上限: 10MB


def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)


def _line_request(method: str, url: str, **kwargs) -> requests.Response:
    """LINE API 呼び出し共通ラッパ。timeout / Authorization / HTTPエラー検知を強制。"""
    kwargs.setdefault("timeout", LINE_TIMEOUT)
    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("Authorization", f"Bearer {CHANNEL_ACCESS_TOKEN}")
    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code >= 400:
        print(f"[LINE] {method} {url} -> {resp.status_code} {resp.text[:300]}", flush=True)
        resp.raise_for_status()
    return resp


def reply(reply_token: str, text: str) -> None:
    _line_request(
        "POST", LINE_REPLY_URL,
        headers={"Content-Type": "application/json"},
        json={"replyToken": reply_token,
              "messages": [{"type": "text", "text": text}]},
    )


def push(user_id: str, text: str) -> None:
    _line_request(
        "POST", LINE_PUSH_URL,
        headers={"Content-Type": "application/json"},
        json={"to": user_id,
              "messages": [{"type": "text", "text": text}]},
    )


def download_image(message_id: str) -> str:
    """LINEから画像を取得してローカル保存。Content-Type / サイズ上限を検証。"""
    url = LINE_CONTENT_URL.format(message_id)
    resp = _line_request("GET", url, stream=True)
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if not content_type.startswith("image/"):
        resp.close()
        raise ValueError(f"unexpected content-type: {content_type!r}")
    declared_len = int(resp.headers.get("Content-Length") or 0)
    if declared_len > MAX_IMAGE_BYTES:
        resp.close()
        raise ValueError(f"image too large: {declared_len} bytes")
    path = os.path.join(IMAGE_DIR, f"{uuid.uuid4()}.jpg")
    written = 0
    try:
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_IMAGE_BYTES:
                    raise ValueError(f"image exceeded max size while streaming: {written}")
                f.write(chunk)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise
    return path

def load_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


# ── 学習者プロファイル ────────────────────────────────────────────────────────

def load_student_profile() -> dict:
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return json.load(f)

def build_profile_context() -> str:
    p = load_student_profile()
    textbooks = "\n".join(f"  - {k}: {v}" for k, v in p.get("textbooks", {}).items()) or "  （未設定）"
    strengths = "、".join(p.get("strengths") or []) or "特になし"
    weaknesses = "、".join(p.get("weaknesses") or []) or "特になし"
    return f"""あなたは経験豊富な家庭教師です。指導相手は以下の小学生です。

【学習者】
- 名前: {p.get("name", "")}
- 学年: 小学{p.get("grade", "")}年生
- 使用教科書:
{textbooks}
- 得意: {strengths}
- 苦手: {weaknesses}
- 備考: {p.get("notes", "")}

【指導方針】
- 学年の既習範囲のみから出題し、未習の概念は問わない
- 丸暗記ではなく理解を問う（「なぜそうなるか」「どう使うか」を重視）
- 苦手分野は丁寧に、得意分野はチャレンジを入れる
- 短時間集中が前提。問題数は少なめで質を重視
"""


# ── Claude ───────────────────────────────────────────────────────────────────

def extract_json(text: str) -> str:
    """Claude応答テキストからJSON領域のみを抽出する。
    マークダウンコードブロック / 前後に説明文がある場合にも対応。"""
    text = text.strip()
    # 1. マークダウンのコードブロック形式
    if "```" in text:
        for part in text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{") or part.startswith("["):
                return part
    # 2. 最後に現れる完結した JSON オブジェクト/配列を探す（カッコの対応を追跡）
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        last_close = text.rfind(close_ch)
        if last_close < 0:
            continue
        depth = 0
        in_string = False
        escape = False
        start = -1
        # last_close から逆向きに対応する open を探す
        for i in range(last_close, -1, -1):
            c = text[i]
            if c == close_ch:
                depth += 1
            elif c == open_ch:
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start >= 0:
            candidate = text[start:last_close + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
    return text


def _collect_text(message) -> str:
    """tool_use などのブロックを除き、text ブロックのみ連結して返す。"""
    parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _parse_json_or_debug(raw_text: str, label: str) -> dict | list:
    """JSONパース失敗時に生テキストをログ出力して再raise。"""
    extracted = extract_json(raw_text)
    try:
        return json.loads(extracted)
    except json.JSONDecodeError:
        print(f"\n[{label}] JSONパース失敗。抽出後テキスト（先頭500文字）:\n{extracted[:500]}\n", flush=True)
        print(f"[{label}] 生テキスト（先頭1000文字）:\n{raw_text[:1000]}\n", flush=True)
        raise


def analyze_step_a(image_paths: list[str],
                   recent_topics_summary: str = "",
                   due_reviews: str = "") -> list[dict]:
    """ステップA: 画像から学習内容を構造化。必要に応じてweb_searchで一般的つまずきを調査。"""
    content = []
    for path in image_paths:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": load_image_b64(path)},
        })
    content.append({"type": "text", "text": f"""{build_profile_context()}

【タスク】
添付画像は本日学校で勉強したノートまたは教科書のページです。以下を分析してください。

【プロセス】
1. 画像から科目・単元を推定する（同じ科目は1つにまとめる）
2. その単元について、必要に応じて web_search ツールで
   「小学{load_student_profile().get('grade','')}年 {{推定単元}} つまずき よくある間違い」
   「{{推定単元}} 指導 ポイント 誤概念」等を検索し、
   一般的に小学生がつまずきやすいポイントを調査する
3. 学習者固有の傾向（苦手・備考）と一般的傾向の双方を統合して stumble_points を作成

【過去30日の学習履歴】
{recent_topics_summary or "（履歴なし）"}

【現在の復習候補】
{due_reviews or "（なし）"}

【出力形式】分析完了後、JSONのみを返してください（マークダウンのコードブロック不要）。
{{
  "subjects": [
    {{
      "subject_name": "算数|国語|理科|社会",
      "unit_guess": "単元名（具体的に）",
      "concept_keys": ["中核となる概念のタグ"],
      "source_summary": "学習内容の要約（2〜3文）",
      "difficulty": "easy|standard|challenging",
      "stumble_points": [
        {{"point": "つまずきの内容", "source": "general|profile"}}
      ],
      "research_notes": "web検索で確認した指導上の注意点の要約（検索しなかった場合は空文字）",
      "links_to_past": [
        {{"topic": "関連する過去の学習", "relation": "前提知識|発展|未学習"}}
      ]
    }}
  ]
}}"""})
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": content}],
    )
    return _parse_json_or_debug(_collect_text(message), "step_a")["subjects"]


def generate_questions_step_b(step_a_output: list[dict],
                              due_reviews_detail: str = "") -> list[dict]:
    """ステップB: 分析結果＋復習候補から問題を生成。"""
    subjects_block = json.dumps(step_a_output, ensure_ascii=False, indent=2)
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": f"""{build_profile_context()}

【タスク】
以下の学習分析と復習候補から、本日の小テストを作ってください。

【本日の学習】
{subjects_block}

【復習すべき項目（過去の誤答・スケジュール復習）】
{due_reviews_detail or "（なし）"}

【出題方針】
1. 本日の学習内容から、各科目につき3問
   - stumble_points の1つ以上を意図的に問う
   - 最低1問は「なぜそうなるか」「どういうときに使うか」を問う応用・推論問題
   - 最低1問は基礎の確認問題
2. 復習候補から最大2問（過去誤答を優先）
   - 誤答した問題そのものではなく、同じ概念を問う別の問題にする
   - 復習問題は、対応する復習候補の `review_topic_id=N` の N をそのまま
     `review_topic_id` フィールドに整数で入れること（推測・創作禁止）
   - 復習候補の科目と本日の学習の科目が一致しない場合は、復習候補の科目で
     新しい subjects エントリを作り、そこに review_questions だけ入れてよい
3. 学習者の学年で答えられる言葉で、問題文は簡潔に（集中力が短い）

【出力形式】JSONのみ（マークダウン不要）。
{{
  "subjects": [
    {{
      "subject_name": "科目名（step_aの subject_name と一致させる）",
      "today_questions": [
        {{
          "q": "問題文",
          "a": "正解",
          "type": "knowledge|application|reasoning",
          "concept_keys": ["概念"],
          "intent": "この問題で何を確認したいか"
        }}
      ],
      "review_questions": [
        {{
          "q": "問題文",
          "a": "正解",
          "type": "knowledge|application|reasoning",
          "concept_keys": ["概念"],
          "intent": "確認したいこと",
          "review_topic_id": null
        }}
      ]
    }}
  ]
}}"""}],
    )
    return _parse_json_or_debug(_collect_text(message), "step_b")["subjects"]


def merge_step_a_b(step_a: list[dict], step_b: list[dict]) -> list[dict]:
    """ステップA/Bの結果を統合し、save_learning_records に渡せる形に整形。"""
    b_by_subject = {s["subject_name"]: s for s in step_b}
    merged = []
    for a in step_a:
        b = b_by_subject.get(a["subject_name"], {"today_questions": [], "review_questions": []})
        today = [{**q, "origin": "today"} for q in b.get("today_questions", [])]
        review = [{**q, "origin": "review"} for q in b.get("review_questions", [])]
        merged.append({
            "subject_name": a["subject_name"],
            "unit_guess": a.get("unit_guess"),
            "concept_keys": a.get("concept_keys", []),
            "summary": a.get("source_summary", ""),
            "difficulty": a.get("difficulty"),
            "stumble_points": a.get("stumble_points", []),
            "questions": today + review,
        })
    return merged


def analyze_all_images(image_paths: list[str], user_id: str | None = None) -> list[dict]:
    """互換ラッパー: step_a → step_b → merge を実行。"""
    history = get_recent_topics_summary(user_id) if user_id else ""
    due = get_due_reviews(user_id) if user_id else []
    step_a = analyze_step_a(
        image_paths,
        recent_topics_summary=history,
        due_reviews=format_due_reviews_brief(due),
    )
    step_b = generate_questions_step_b(
        step_a,
        due_reviews_detail=format_due_reviews_for_prompt(due),
    )
    return merge_step_a_b(step_a, step_b)

def grade_answers(image_path: str, questions: list) -> list[dict]:
    image_b64 = load_image_b64(image_path)
    qa_lines = []
    for i, q in enumerate(questions, 1):
        qa_lines.append(f"Q{i}. {q.get('q', '')}  正解: {q.get('a', '')}")
        if q.get("intent"):
            qa_lines.append(f"    出題意図: {q['intent']}")
        if q.get("concept_keys"):
            qa_lines.append(f"    概念: {', '.join(q['concept_keys'])}")
        if q.get("type"):
            qa_lines.append(f"    種別: {q['type']}")
    qa_text = "\n".join(qa_lines)
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": f"""{build_profile_context()}

この学習者が先ほどの小テストに答えたノートです。採点してください。

【採点方針】
- 意味が合っていれば正解とし、表記の細かい違いは許容する
- 誤答時のコメントは「ヒントのみ」。答えそのものや答えを直接示唆する表現は使わない
- 学習者の学年・苦手傾向を踏まえ、優しく具体的に励ます
- 誤答は原因を分類する（calc_error / concept_error / read_error / unknown / partial）

【問題と正解】
{qa_text}

【出力形式】JSON配列のみ（マークダウン不要）。
[
  {{
    "q": "問題文",
    "student_answer": "生徒の答え（読み取れない場合は 空文字 または 推測）",
    "correct": true,
    "mistake_category": "calc_error|concept_error|read_error|unknown|partial（正解時はnull）",
    "concept_keys": ["関連する概念タグ"],
    "comment": "小学生向けの一言（誤答時は答えを示さずヒントのみ）",
    "teaching_note": "保護者向けメモ：何が分かっていて何が分かっていないかを簡潔に"
  }}
]"""},
        ]}],
    )
    return _parse_json_or_debug(_collect_text(message), "grade_answers")

def generate_daily_report(records: list[dict], balance: int,
                          user_id: str | None = None) -> str:
    if not records:
        return "本日の学習記録はありません。"
    summary_text = "\n".join(
        f"・{r['subject_name']}「{r.get('unit') or ''}」: {r['score']}/{r['total']}問正解"
        f" — {r['summary'][:40]}"
        for r in records
    )
    total_score = sum(r["score"] for r in records)
    total_q = sum(r["total"] for r in records)

    weak_block = "（データなし）"
    notes_block = "（データなし）"
    if user_id:
        weak_block = format_weak_points_block(get_weak_points(user_id, days=7, limit=3))
        notes = get_recent_teaching_notes(user_id, days=1, limit=5)
        if notes:
            notes_block = "\n".join(
                f"- {n['subject']}「{n['unit']}」"
                f" {'○' if n['is_correct'] else '×'}"
                f" {n['teaching_note']}"
                for n in notes
            )

    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""{build_profile_context()}

この学習者の保護者向けに、本日の学習レポートを作成してください。

【本日の学習記録】
{summary_text}
合計: {total_score}/{total_q}問正解 / クレジット残高: {balance}

【本日の所見（採点メモ抜粋）】
{notes_block}

【直近1週間の弱点トップ3】
{weak_block}

【書き方】
- 200〜300文字の日本語で、保護者が読みやすい箇条書き中心
- 次の3点を簡潔に:
  1) 本日の総評（どの単元がどの程度できたか・具体的な褒めどころ）
  2) 気になる誤答の傾向（弱点ブロックと所見から1点だけ選んで深掘り）
  3) 明日やってほしい具体的な声かけ・取り組み（1つだけ、実行可能なもの）
- 丸暗記ではなく「なぜ」を問う家庭教師の目線を保つこと"""}],
    )
    return _collect_text(message).strip()


def generate_weekly_report(records: list[dict], balance: int,
                           user_id: str | None = None) -> str:
    if not records:
        return "今週の学習記録はありません。"
    by_day: dict[str, list] = {}
    for r in records:
        by_day.setdefault(r["day"], []).append(r)
    summary_lines = []
    for day, items in sorted(by_day.items()):
        subjects = "、".join(
            f"{i['subject_name']}「{i.get('unit') or ''}」({i['score']}/{i['total']})"
            for i in items
        )
        summary_lines.append(f"{day}: {subjects}")

    trend_block = "（推移データなし）"
    weak_block = "（データなし）"
    notes_block = "（データなし）"
    if user_id:
        trend_block = format_mastery_trend_block(get_mastery_trend(user_id, days=7))
        weak_block = format_weak_points_block(get_weak_points(user_id, days=7, limit=3))
        notes = get_recent_teaching_notes(user_id, days=7, limit=8)
        if notes:
            notes_block = "\n".join(
                f"- {n['subject']}「{n['unit']}」"
                f" {'○' if n['is_correct'] else '×'} {n['teaching_note']}"
                for n in notes
            )

    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=900,
        messages=[{"role": "user", "content": f"""{build_profile_context()}

この学習者の保護者向けに、今週の学習週次レポートを作成してください。

【今週の学習記録】
{chr(10).join(summary_lines)}
クレジット残高: {balance}

【単元別・定着度推移（今週 vs 前週）】
{trend_block}

【今週の弱点トップ3】
{weak_block}

【採点メモ抜粋（teaching_note, 誤答優先）】
{notes_block}

【書き方】
- 400〜500文字の日本語、保護者が読みやすい見出し付き箇条書き
- 次の4節で構成すること（見出しは絵文字可）:
  1) ✨ 今週の成長: 伸びた単元（delta>0）や継続できたことを具体名で褒める
  2) ⚠️ 要補強の単元: 弱点トップ3から特に優先すべき1〜2点を、誤答の原因
     （calc_error/concept_error/read_error など）に踏み込んで説明
  3) 🧠 指導のポイント: 家庭教師としての助言。「なぜそうなるか」を問う視点で、
     単元Xには具体的にこう教える／声かけするという指針を書く
  4) 🎯 来週の方針: 来週の最初に取り組むべき具体的な1ステップ（実行可能で小さく）
- 本人の学年で既習の語彙を使う
"""}],
    )
    return _collect_text(message).strip()


# ── 景品 ─────────────────────────────────────────────────────────────────────

def load_prizes() -> list[dict]:
    with open(PRIZES_PATH, encoding="utf-8") as f:
        return json.load(f)

def format_prize_catalog(prizes: list[dict], balance: int) -> str:
    lines = [f"【景品カタログ】現在の残高: {balance}クレジット\n"]
    for p in prizes:
        mark = "✓" if balance >= p["cost"] else "×"
        lines.append(f"{p['id']}. {p['name']} ({p['cost']}cr) {mark}")
    lines.append("\n「交換 番号」で申請できます（例: 交換 1）")
    return "\n".join(lines)


# ── レポート送信（スケジューラから呼ばれる）────────────────────────────────────

def send_daily_report() -> None:
    today = date.today().isoformat()
    records = get_daily_summary(CHILD_USER_ID, today)
    balance = get_credits(CHILD_USER_ID)
    report = generate_daily_report(records, balance, user_id=CHILD_USER_ID)
    header = f"📊 【日次レポート】{today}\n\n"
    push(PARENT_USER_ID, header + report)

def send_weekly_report() -> None:
    records = get_weekly_summary(CHILD_USER_ID)
    balance = get_credits(CHILD_USER_ID)
    report = generate_weekly_report(records, balance, user_id=CHILD_USER_ID)
    today = date.today().isoformat()
    header = f"📅 【週次レポート】{today}\n\n"
    push(PARENT_USER_ID, header + report)


# ── メッセージハンドラ ────────────────────────────────────────────────────────

def handle_child(user_id: str, reply_token: str, msg: dict) -> None:
    session = get_active_session(user_id)

    if msg.get("type") == "text":
        text = msg["text"].strip()

        if text == "残高":
            balance = get_credits(user_id)
            reply(reply_token, f"現在のクレジット残高: {balance}cr")

        elif text == "交換":
            prizes = load_prizes()
            balance = get_credits(user_id)
            reply(reply_token, format_prize_catalog(prizes, balance))

        elif text.startswith("交換 "):
            try:
                prize_id = int(text.split()[1])
                prizes = load_prizes()
                prize = next((p for p in prizes if p["id"] == prize_id), None)
                if prize is None:
                    reply(reply_token, "その番号の景品はありません。")
                    return
                balance = get_credits(user_id)
                if balance < prize["cost"]:
                    reply(reply_token, f"クレジットが足りません。必要: {prize['cost']}cr / 残高: {balance}cr")
                    return
                exchange_id = create_exchange(user_id, prize["name"], prize["cost"])
                reply(reply_token, f"「{prize['name']}」の交換申請を送りました！\n保護者の承認をお待ちください。")
                push(PARENT_USER_ID,
                     f"【景品交換申請 #{exchange_id}】\n"
                     f"景品: {prize['name']} ({prize['cost']}cr)\n"
                     f"「承認 {exchange_id}」または「却下 {exchange_id}」で返答してください。")
            except (IndexError, ValueError):
                reply(reply_token, "「交換 番号」の形式で入力してください。例: 交換 1")

        elif text in ("おわり", "終わり", "テスト作って", "テスト"):
            if session is None or session["status"] != "collecting":
                reply(reply_token, "まず勉強したページの写真を送ってください📷")
                return
            image_paths = get_session_images(session["id"])
            if not image_paths:
                reply(reply_token, "写真が届いていません。ノートや教科書の写真を送ってください📷")
                return
            reply(reply_token, f"{len(image_paths)}枚の写真を受け取りました。学習内容を分析してテストを作成中です...📝")
            history = get_recent_topics_summary(user_id)
            due = get_due_reviews(user_id)
            step_a = analyze_step_a(
                image_paths,
                recent_topics_summary=history,
                due_reviews=format_due_reviews_brief(due),
            )
            step_b = generate_questions_step_b(
                step_a,
                due_reviews_detail=format_due_reviews_for_prompt(due),
            )
            subjects_data = merge_step_a_b(step_a, step_b)
            save_learning_records(session["id"], user_id, subjects_data)
            set_session_status(session["id"], "grading")
            for i, s in enumerate(subjects_data, 1):
                today_qs = [q for q in s["questions"] if q.get("origin") != "review"]
                review_qs = [q for q in s["questions"] if q.get("origin") == "review"]
                lines = [
                    f"【{s['subject_name']}】({i}/{len(subjects_data)}科目)",
                    f"📖 {s.get('summary', '')}\n",
                    "【今日のテスト】ノートに答えを書いて写真を送ってね！\n",
                ]
                n = 0
                for q in today_qs:
                    n += 1
                    lines.append(f"Q{n}. {q['q']}")
                if review_qs:
                    lines.append("\n【おさらい】以前の内容からも出題！")
                    for q in review_qs:
                        n += 1
                        lines.append(f"Q{n}. {q['q']}")
                push(user_id, "\n".join(lines))
            push(user_id, "全科目のテストを送りました！\nノートに解答を書いて、写真を送ってください📷")

        else:
            reply(reply_token, "「残高」「交換」「おわり」などのコマンドが使えます。\n勉強したページの写真を送ることもできます📷")
        return

    if msg.get("type") != "image":
        return

    if session is None or session["status"] == "collecting":
        if session is None:
            session = {"id": create_session(user_id), "status": "collecting"}
        image_path = download_image(msg["id"])
        add_session_image(session["id"], image_path)
        reply(reply_token, "写真を受け取りました📷\n他の科目の写真も続けて送れます。全部送ったら「おわり」と入力してください。")

    elif session["status"] == "grading":
        subject = get_next_unanswered(session["id"])
        if subject is None:
            reply(reply_token, "すべての科目の解答が完了しています！お疲れさまでした🎉")
            set_session_status(session["id"], "done")
            return
        subject_name = subject.get("subject_name") or "テスト"
        reply(reply_token, f"【{subject_name}】の解答を受信しました。採点中です...✏️")
        image_path = download_image(msg["id"])
        results = grade_answers(image_path, subject["questions"])
        score = sum(1 for r in results if r.get("correct"))
        earned = score * CREDIT_PER_CORRECT
        balance = add_credits(user_id, earned)
        apply_grading_results(user_id, subject["topic_id"], subject["id"],
                              subject["questions"], results)
        complete_learning_record(subject["id"], score)

        lines = [f"【{subject_name} 採点結果】 {score}/{subject['total']}問正解 🎉\n"]
        for i, r in enumerate(results, 1):
            mark = "○" if r.get("correct") else "×"
            lines.append(f"{mark} Q{i}. {r.get('q', '')}")
            lines.append(f"   あなたの答え: {r.get('student_answer', '？')}")
            if r.get("comment"):
                lines.append(f"   {r['comment']}")
            lines.append("")
        lines.append(f"+{earned}クレジット獲得！（残高: {balance}cr）")
        push(user_id, "\n".join(lines))

        remaining = count_waiting_records(session["id"])
        if remaining > 0:
            push(user_id, f"残り{remaining}科目あります。次の解答写真を送ってください📷")
        else:
            set_session_status(session["id"], "done")
            push(user_id, "全科目の採点完了！今日もよく頑張りました🌟")


def handle_parent(user_id: str, reply_token: str, msg: dict) -> None:
    if msg.get("type") != "text":
        reply(reply_token, "保護者用コマンド: 「レポート」「週次」「承認 ID」「却下 ID」「申請一覧」")
        return

    text = msg["text"].strip()

    if text == "レポート":
        today = date.today().isoformat()
        records = get_daily_summary(CHILD_USER_ID, today)
        balance = get_credits(CHILD_USER_ID)
        report = generate_daily_report(records, balance, user_id=CHILD_USER_ID)
        reply(reply_token, f"📊 【日次レポート】{today}\n\n{report}")

    elif text == "週次":
        records = get_weekly_summary(CHILD_USER_ID)
        balance = get_credits(CHILD_USER_ID)
        report = generate_weekly_report(records, balance, user_id=CHILD_USER_ID)
        reply(reply_token, f"📅 【週次レポート】\n\n{report}")

    elif text == "申請一覧":
        exchanges = get_pending_exchanges()
        if not exchanges:
            reply(reply_token, "現在、未処理の景品交換申請はありません。")
        else:
            lines = ["【未処理の景品交換申請】"]
            for e in exchanges:
                lines.append(f"#{e['id']} {e['prize_name']} ({e['cost']}cr) — {e['created_at'][:10]}")
            lines.append("\n「承認 ID」または「却下 ID」で処理できます。")
            reply(reply_token, "\n".join(lines))

    elif text.startswith("承認 ") or text.startswith("却下 "):
        action, *rest = text.split()
        try:
            exchange_id = int(rest[0])
            status = "approved" if action == "承認" else "rejected"
            exchange = update_exchange_status(exchange_id, status)
            if exchange is None:
                reply(reply_token, f"申請 #{exchange_id} が見つかりません。")
                return
            if status == "approved":
                new_balance = deduct_credits(CHILD_USER_ID, exchange["cost"])
                reply(reply_token, f"申請 #{exchange_id}「{exchange['prize_name']}」を承認しました。\n（{exchange['cost']}cr 差し引き、残高: {new_balance}cr）")
                push(CHILD_USER_ID, f"景品「{exchange['prize_name']}」の交換が承認されました！\n残高: {new_balance}cr")
            else:
                reply(reply_token, f"申請 #{exchange_id}「{exchange['prize_name']}」を却下しました。")
                push(CHILD_USER_ID, f"景品「{exchange['prize_name']}」の交換申請が却下されました。保護者に確認してみてください。")
        except (IndexError, ValueError):
            reply(reply_token, "「承認 番号」の形式で入力してください。例: 承認 1")

    else:
        reply(reply_token, "保護者用コマンド:\n・「レポート」: 本日の学習レポート\n・「週次」: 今週のレポート\n・「申請一覧」: 未処理の景品交換申請\n・「承認 ID」/「却下 ID」: 交換申請の処理")


# ── Webhook ──────────────────────────────────────────────────────────────────

# Claude呼び出しで長時間かかる処理を背景で走らせるためのエグゼキュータ。
# LINE webhook は即 200 OK を返し、再送による二重処理を避ける。
_webhook_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")


def record_webhook_event(event_id: str) -> bool:
    """webhook_events に event_id を INSERT。新規なら True、重複なら False。"""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO webhook_events (event_id) VALUES (?)",
            (event_id,),
        )
        return cur.rowcount > 0


def _process_event(event: dict) -> None:
    """1イベントをディスパッチ。例外はログするがプロセスを落とさない。"""
    if event.get("type") != "message":
        return
    user_id = event["source"]["userId"]
    reply_token = event["replyToken"]
    msg = event["message"]
    try:
        if user_id == PARENT_USER_ID:
            handle_parent(user_id, reply_token, msg)
        elif user_id == CHILD_USER_ID:
            handle_child(user_id, reply_token, msg)
        else:
            reply(reply_token, f"このボットは登録済みのユーザーのみ使用できます。\nあなたのID: {user_id}")
    except Exception:
        traceback.print_exc()
        try:
            push(user_id, "エラーが発生しました。もう一度試してください。")
        except Exception:
            traceback.print_exc()


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()
    if not verify_signature(body, signature):
        abort(400)

    for event in json.loads(body).get("events", []):
        event_id = event.get("webhookEventId")
        if event_id and not record_webhook_event(event_id):
            # 同じイベントを既に処理済み → LINE再送/重複配信を弾く
            continue
        _webhook_executor.submit(_process_event, event)

    return "OK", 200


# ── スケジューラ ──────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    scheduler = BackgroundScheduler()
    # 毎日 REPORT_HOUR 時に日次レポート
    scheduler.add_job(send_daily_report, "cron", hour=REPORT_HOUR, minute=0)
    # 毎週日曜 REPORT_HOUR+1 時に週次レポート
    scheduler.add_job(send_weekly_report, "cron", day_of_week="sun", hour=REPORT_HOUR + 1, minute=0)
    scheduler.start()


if __name__ == "__main__":
    from waitress import serve
    init_db()
    start_scheduler()
    serve(app, host="127.0.0.1", port=5000)
