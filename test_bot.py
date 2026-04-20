"""
LINEなしでボットのコア機能を検証するテストスクリプト。
DBとClaude APIを実際に使って動作確認する。
"""
import json
import os
import sqlite3
import sys

# テスト用DBを別ファイルに
os.environ.setdefault("DB_PATH_OVERRIDE", os.path.join(os.path.dirname(__file__), "test_bot.db"))

# bot.pyのDB_PATHをテスト用に差し替え
import importlib
import bot
# テスト用DB
TEST_DB = os.path.join(os.path.dirname(__file__), "test_bot.db")
bot.DB_PATH = TEST_DB

IMAGE_DIR = os.path.join(os.path.dirname(__file__), "images")
MATH1 = os.path.join(IMAGE_DIR, "math_page1.jpg")
MATH2 = os.path.join(IMAGE_DIR, "math_page2.jpg")
JAPANESE = os.path.join(IMAGE_DIR, "japanese_page.jpg")
ANSWER = os.path.join(IMAGE_DIR, "answer_page.jpg")
TEST_USER = "U_test_user_001"

passed = 0
failed = 0

def ok(label: str):
    global passed
    passed += 1
    print(f"  ✅ {label}")

def ng(label: str, err: str):
    global failed
    failed += 1
    print(f"  ❌ {label}: {err}")


# ─────────────────────────────────────────────
print("\n=== 1. DB初期化 ===")
try:
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    bot.init_db()
    with sqlite3.connect(TEST_DB) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ("sessions", "session_images", "topics", "learning_records",
              "question_attempts", "review_queue", "credits", "exchanges"):
        assert t in tables, f"table {t} missing"
    ok("テーブル作成")
except Exception as e:
    ng("テーブル作成", str(e))
    sys.exit(1)


# ─────────────────────────────────────────────
print("\n=== 2. セッション・画像登録 ===")
try:
    session_id = bot.create_session(TEST_USER)
    assert session_id > 0
    ok(f"セッション作成 (id={session_id})")

    bot.add_session_image(session_id, MATH1)
    bot.add_session_image(session_id, MATH2)
    bot.add_session_image(session_id, JAPANESE)
    paths = bot.get_session_images(session_id)
    assert len(paths) == 3
    ok(f"画像登録 ({len(paths)}枚)")
except Exception as e:
    ng("セッション・画像登録", str(e))
    sys.exit(1)


# ─────────────────────────────────────────────
print("\n=== 3a. Claude: ステップA (画像分析・つまずき調査) ===")
print("  (Claude APIを呼び出し中... web検索を含むため時間がかかります)")
try:
    step_a = bot.analyze_step_a(paths)
    assert isinstance(step_a, list) and len(step_a) >= 1
    ok(f"ステップA: {len(step_a)}科目検出")
    for a in step_a:
        assert "subject_name" in a
        assert "unit_guess" in a
        assert "source_summary" in a
        assert "stumble_points" in a
        sp_count = len(a.get("stumble_points", []))
        rn = a.get("research_notes", "")
        ok(f"  「{a['subject_name']}」単元={a.get('unit_guess')} / つまずき{sp_count}件 / 調査メモ={(rn[:30] + '...') if rn else '(なし)'}")
except Exception as e:
    ng("ステップA", str(e))
    sys.exit(1)


print("\n=== 3b. Claude: ステップB (小テスト生成) ===")
print("  (Claude APIを呼び出し中...)")
try:
    step_b = bot.generate_questions_step_b(step_a)
    assert isinstance(step_b, list) and len(step_b) >= 1
    for b in step_b:
        assert "subject_name" in b
        assert isinstance(b.get("today_questions"), list)
        assert len(b["today_questions"]) >= 1
        # intent と concept_keys が付いていること
        q0 = b["today_questions"][0]
        assert "intent" in q0
        assert "type" in q0
    ok(f"ステップB: 科目ごとに今日の問題生成 (intent/type/concept_keys付き)")
except Exception as e:
    ng("ステップB", str(e))
    sys.exit(1)


print("\n=== 3c. マージと保存用データ作成 ===")
try:
    subjects_data = bot.merge_step_a_b(step_a, step_b)
    assert len(subjects_data) == len(step_a)
    for s in subjects_data:
        assert "subject_name" in s
        assert "summary" in s
        assert "questions" in s
        assert "stumble_points" in s
        assert "difficulty" in s
        assert len(s["questions"]) >= 1
        ok(f"  マージ後「{s['subject_name']}」: 全{len(s['questions'])}問, 難易度={s.get('difficulty')}")
except Exception as e:
    ng("マージ", str(e))
    sys.exit(1)


# ─────────────────────────────────────────────
print("\n=== 4. DB: 学習記録保存・トピック作成・取得 ===")
try:
    bot.save_learning_records(session_id, TEST_USER, subjects_data)
    bot.set_session_status(session_id, "grading")
    subject = bot.get_next_unanswered(session_id)
    assert subject is not None
    assert subject.get("subject_name") is not None
    assert subject.get("topic_id") is not None
    assert isinstance(subject["questions"], list)
    with sqlite3.connect(TEST_DB) as conn:
        topic_count = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        assert topic_count >= 1
    ok(f"学習記録・トピック作成: 「{subject['subject_name']}」 (topic_id={subject['topic_id']}, topics={topic_count}件)")
except Exception as e:
    ng("DB操作", str(e))
    sys.exit(1)


# ─────────────────────────────────────────────
print("\n=== 5. Claude: 解答採点 ===")
print("  (Claude APIを呼び出し中...)")
try:
    results = bot.grade_answers(ANSWER, subject["questions"])
    assert isinstance(results, list)
    assert len(results) == len(subject["questions"])
    score = sum(1 for r in results if r.get("correct"))
    # 誤答が1件でもあれば、いずれかに mistake_category / teaching_note がセットされていること
    wrong = [r for r in results if not r.get("correct")]
    if wrong:
        assert any(r.get("mistake_category") for r in wrong), "誤答があるが mistake_category が全件欠落"
    assert any(r.get("teaching_note") for r in results), "teaching_note がすべて欠落"
    ok(f"採点完了: {score}/{len(results)}問正解 (誤答分類・teaching_note付き)")
    for i, r in enumerate(results, 1):
        mark = "⭕" if r.get("correct") else "❌"
        cat = r.get("mistake_category") or ""
        print(f"    {mark} Q{i} [{cat}]: student='{r.get('student_answer','')}' / comment='{r.get('comment','')}'")
        if r.get("teaching_note"):
            print(f"       📝 {r['teaching_note'][:80]}")
except Exception as e:
    ng("採点", str(e))
    sys.exit(1)


# ─────────────────────────────────────────────
print("\n=== 6. クレジット付与 ===")
try:
    earned = score * bot.CREDIT_PER_CORRECT
    balance = bot.add_credits(TEST_USER, earned)
    assert balance == earned
    ok(f"+{earned}クレジット付与 (残高={balance})")

    # 追加付与
    balance2 = bot.add_credits(TEST_USER, 20)
    assert balance2 == earned + 20
    ok(f"+20クレジット追加 (残高={balance2})")
except Exception as e:
    ng("クレジット", str(e))


# ─────────────────────────────────────────────
print("\n=== 7. 問題単位の結果保存・科目完了 ===")
try:
    # ハンドラと同様のフック順序で実行
    bot.save_question_attempts(subject["id"], subject["topic_id"], subject["questions"], results)
    bot.update_review_queue_from_result(TEST_USER, subject["topic_id"], subject["questions"], results)
    new_mastery = bot.update_topic_mastery(subject["topic_id"], score, len(results))
    with sqlite3.connect(TEST_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM question_attempts WHERE learning_record_id=?",
            (subject["id"],),
        ).fetchall()
        assert len(rows) == len(subject["questions"])
        # 新規カラムの存在と書き込み確認
        assert any(r["intent"] for r in rows), "intent がすべて欠落"
        assert any(r["teaching_note"] for r in rows), "teaching_note がすべて欠落"
        origins = {r["origin"] for r in rows}
        assert origins.issubset({"today", "review"})
    ok(f"question_attempts保存: {len(rows)}件 (intent/teaching_note/origin付き, origins={origins})")

    # review_queue: 誤答数だけエントリが作成されている（concept_keyが同一だと統合される点に注意）
    wrong_count = sum(1 for r in results if not r.get("correct"))
    import datetime as _dt
    tomorrow = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    with sqlite3.connect(TEST_DB) as conn:
        conn.row_factory = sqlite3.Row
        rq_rows = conn.execute(
            "SELECT * FROM review_queue WHERE user_id=? AND topic_id=?",
            (TEST_USER, subject["topic_id"]),
        ).fetchall()
    if wrong_count > 0:
        assert len(rq_rows) >= 1, "誤答があるのに review_queue が空"
        for r in rq_rows:
            assert r["reason"] == "mistake", f"reason不正: {dict(r)}"
            assert r["scheduled_for"] == tomorrow, f"scheduled_for不正: {dict(r)}, 期待={tomorrow}"
            assert r["interval_days"] == 1, f"interval_days不正: {dict(r)}"
            assert r["status"] == "pending", f"status不正: {dict(r)}"
            assert r["last_result"] == "incorrect", f"last_result不正: {dict(r)}"
        ok(f"review_queue: 誤答{wrong_count}問 → {len(rq_rows)}件登録 (scheduled_for={tomorrow}, interval=1)")
    else:
        ok("review_queue: 誤答なし → エントリ作成なし")

    # mastery 更新の検証: α=0.4, 初期値0.0, ratio=score/total なので mastery ≈ 0.4*ratio
    expected_mastery = 0.4 * (score / len(results))
    assert abs(new_mastery - expected_mastery) < 1e-9, f"mastery計算不一致: new={new_mastery}, expected={expected_mastery}"
    ok(f"topics.mastery 更新: {new_mastery:.3f} (期待値 {expected_mastery:.3f}, α=0.4)")

    bot.complete_learning_record(subject["id"], score)
    remaining = bot.count_waiting_records(session_id)
    ok(f"学習記録完了 (残り{remaining}件)")

    # 全記録を完了させる
    while True:
        nxt = bot.get_next_unanswered(session_id)
        if nxt is None:
            break
        bot.complete_learning_record(nxt["id"], 2)

    bot.set_session_status(session_id, "done")
    session = bot.get_active_session(TEST_USER)
    assert session is None, f"done後も active session が返る: {session}"
    ok("セッション完了")
except Exception as e:
    ng("セッション完了", str(e))


# ─────────────────────────────────────────────
print("\n=== 8. 復習キュー昇格ロジック（単体） ===")
try:
    # 専用のユーザー・トピックで、正答を繰り返したときの間隔遷移を検証
    UNIT_USER = "U_unit_test_002"
    unit_topic_id = bot.get_or_create_topic("テスト科目", "テスト単元")
    # 初期状態: 誤答で1日後エントリ作成
    sample_q = [{"q": "テスト問題", "a": "答え", "concept_keys": ["テスト概念"]}]
    bot.update_review_queue_from_result(UNIT_USER, unit_topic_id, sample_q, [{"correct": False}])
    with sqlite3.connect(TEST_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM review_queue WHERE user_id=? AND topic_id=? ORDER BY id DESC LIMIT 1",
            (UNIT_USER, unit_topic_id),
        ).fetchone()
    assert row["interval_days"] == 1 and row["status"] == "pending"
    ok(f"初期誤答: interval=1, status=pending")

    # 正答を繰り返し、1→3→7→14→30→retired と遷移することを確認
    expected_seq = [3, 7, 14, 30, "retired"]
    for expected in expected_seq:
        bot.update_review_queue_from_result(UNIT_USER, unit_topic_id, sample_q, [{"correct": True}])
        with sqlite3.connect(TEST_DB) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM review_queue WHERE user_id=? AND topic_id=? ORDER BY id DESC LIMIT 1",
                (UNIT_USER, unit_topic_id),
            ).fetchone()
        if expected == "retired":
            assert row["status"] == "retired", f"30日超えで retired になっていない: status={row['status']}"
            ok(f"30日超え → status=retired, times_reviewed={row['times_reviewed']}")
        else:
            assert row["interval_days"] == expected, f"interval期待値 {expected} != 実際 {row['interval_days']}"
            assert row["status"] == "pending"
            assert row["last_result"] == "correct"
            ok(f"正答後: interval={expected}日")

    # 再誤答で1日にリセット（retired後は別エントリが必要なので手動で pending 復活）
    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO review_queue (user_id, topic_id, concept_key, reason, scheduled_for, interval_days, status) "
            "VALUES (?,?,?,?,DATE('now','+7 day'),7,'pending')",
            (UNIT_USER, unit_topic_id, "テスト概念2", "mistake"),
        )
    bot.update_review_queue_from_result(
        UNIT_USER, unit_topic_id,
        [{"q": "再テスト", "a": "x", "concept_keys": ["テスト概念2"]}],
        [{"correct": False}],
    )
    with sqlite3.connect(TEST_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM review_queue WHERE user_id=? AND concept_key='テスト概念2' ORDER BY id DESC LIMIT 1",
            (UNIT_USER,),
        ).fetchone()
    assert row["interval_days"] == 1 and row["last_result"] == "incorrect"
    ok("再誤答でintervalが1日にリセット")
except Exception as e:
    ng("review_queue昇格", str(e))


print("\n=== 9. get_due_reviews (P5用の取得) ===")
try:
    due = bot.get_due_reviews(TEST_USER)
    # TEST_USERの誤答分が今日+1日で登録されているので、今日の日付では該当なし（明日以降の日付で返る想定）
    assert isinstance(due, list)
    # 明日の日付を渡せば今日登録した誤答が取れる
    tomorrow_iso = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    due_tomorrow = bot.get_due_reviews(TEST_USER, today_iso=tomorrow_iso)
    assert all(r["scheduled_for"] <= tomorrow_iso for r in due_tomorrow)
    ok(f"get_due_reviews: 今日={len(due)}件 / 明日時点={len(due_tomorrow)}件")
except Exception as e:
    ng("get_due_reviews", str(e))


# ─────────────────────────────────────────────
print("\n=== 10. format_due_reviews_* (プロンプト整形) ===")
try:
    ROUTE_USER = "U_route_user_003"
    # 2トピック作成
    tid_math = bot.get_or_create_topic("算数", "分数のかけ算", ["分数×分数"])
    tid_kokugo = bot.get_or_create_topic("国語", "熟語の構成", ["熟語"])

    # 算数: 誤答で review_queue エントリを作り、question_attempts にも直近誤答を残す
    session_id_r = bot.create_session(ROUTE_USER)
    import json as _json
    with sqlite3.connect(TEST_DB) as conn:
        lr_math = conn.execute(
            """INSERT INTO learning_records (session_id, topic_id, user_id,
               source_summary, questions, total) VALUES (?,?,?,?,?,?)""",
            (session_id_r, tid_math, ROUTE_USER, "分数のかけ算",
             _json.dumps([]), 0),
        ).lastrowid
    # 直近誤答を記録（concept_keys に "分数×分数" を含む）
    bot.save_question_attempts(
        lr_math, tid_math,
        [{"q": "2/3 × 3/4 は？", "a": "1/2", "concept_keys": ["分数×分数"],
          "type": "knowledge", "intent": "基本計算", "origin": "today"}],
        [{"correct": False, "student_answer": "6/12", "mistake_category": "calc_error",
          "teaching_note": "約分し忘れ"}],
    )
    bot.update_review_queue_from_result(
        ROUTE_USER, tid_math,
        [{"q": "2/3 × 3/4 は？", "a": "1/2", "concept_keys": ["分数×分数"]}],
        [{"correct": False}],
    )

    tomorrow_iso = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    due = bot.get_due_reviews(ROUTE_USER, today_iso=tomorrow_iso)
    assert len(due) >= 1
    brief = bot.format_due_reviews_brief(due)
    detail = bot.format_due_reviews_for_prompt(due)
    assert "算数" in brief and "分数のかけ算" in brief
    assert f"review_topic_id={tid_math}" in detail, \
        f"detail に review_topic_id マーカーが無い: {detail}"
    assert "前回誤答例" in detail, "直近誤答例が integrated されていない"
    ok(f"format_due_reviews_brief/for_prompt: review_topic_id マーカーと誤答例を含む")
except Exception as e:
    ng("format_due_reviews_*", str(e))


# ─────────────────────────────────────────────
print("\n=== 11. apply_grading_results: 復習問題のルーティング ===")
try:
    # 本日のセッションは「国語」、復習問題は「算数」(tid_math) に紐づけ
    with sqlite3.connect(TEST_DB) as conn:
        lr_kokugo = conn.execute(
            """INSERT INTO learning_records (session_id, topic_id, user_id,
               source_summary, questions, total) VALUES (?,?,?,?,?,?)""",
            (session_id_r, tid_kokugo, ROUTE_USER, "熟語の構成",
             _json.dumps([]), 0),
        ).lastrowid

    mixed_questions = [
        # 本日(国語) — 正答
        {"q": "上下", "a": "対義", "concept_keys": ["熟語"], "type": "knowledge",
         "intent": "熟語の意味関係", "origin": "today"},
        # 本日(国語) — 誤答
        {"q": "再会", "a": "同意", "concept_keys": ["熟語"], "type": "knowledge",
         "intent": "熟語の意味関係", "origin": "today"},
        # 復習(算数) — 誤答 (review_topic_id=tid_math)
        {"q": "1/2 × 4/5 は？", "a": "2/5", "concept_keys": ["分数×分数"],
         "type": "knowledge", "intent": "復習: 分数のかけ算",
         "origin": "review", "review_topic_id": tid_math},
    ]
    mixed_results = [
        {"correct": True, "student_answer": "対義"},
        {"correct": False, "student_answer": "反対", "mistake_category": "concept_error",
         "teaching_note": "同義と対義の混同"},
        {"correct": False, "student_answer": "4/10", "mistake_category": "calc_error",
         "teaching_note": "約分できていない"},
    ]
    summary = bot.apply_grading_results(
        ROUTE_USER, tid_kokugo, lr_kokugo, mixed_questions, mixed_results
    )

    # (1) question_attempts が各問で正しい topic_id に入ったか
    with sqlite3.connect(TEST_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT question_text, topic_id, origin FROM question_attempts "
            "WHERE learning_record_id=? ORDER BY id",
            (lr_kokugo,),
        ).fetchall()
    assert len(rows) == 3
    assert rows[0]["topic_id"] == tid_kokugo and rows[0]["origin"] == "today"
    assert rows[1]["topic_id"] == tid_kokugo and rows[1]["origin"] == "today"
    assert rows[2]["topic_id"] == tid_math and rows[2]["origin"] == "review", \
        f"復習問題が復習元トピックに紐づいていない: topic_id={rows[2]['topic_id']}"
    ok("question_attempts: 復習問題は review_topic_id 側に保存された")

    # (2) review_queue: 復習誤答により tid_math の該当エントリは interval=1 にリセット/維持
    with sqlite3.connect(TEST_DB) as conn:
        conn.row_factory = sqlite3.Row
        rq_math = conn.execute(
            "SELECT * FROM review_queue WHERE user_id=? AND topic_id=? ORDER BY id DESC LIMIT 1",
            (ROUTE_USER, tid_math),
        ).fetchone()
        # 国語の誤答があるので tid_kokugo にも pending エントリが1件
        rq_kokugo = conn.execute(
            "SELECT * FROM review_queue WHERE user_id=? AND topic_id=? AND status='pending'",
            (ROUTE_USER, tid_kokugo),
        ).fetchall()
    assert rq_math["interval_days"] == 1 and rq_math["last_result"] == "incorrect"
    assert len(rq_kokugo) == 1 and rq_kokugo[0]["interval_days"] == 1
    ok("review_queue: 本日・復習それぞれのトピックに誤答が反映された")

    # (3) mastery: tid_math と tid_kokugo 両方が更新されている
    assert tid_math in summary and tid_kokugo in summary
    assert summary[tid_math]["total"] == 1 and summary[tid_math]["score"] == 0
    assert summary[tid_kokugo]["total"] == 2 and summary[tid_kokugo]["score"] == 1
    with sqlite3.connect(TEST_DB) as conn:
        conn.row_factory = sqlite3.Row
        m_math = conn.execute("SELECT mastery FROM topics WHERE id=?", (tid_math,)).fetchone()["mastery"]
        m_kokugo = conn.execute("SELECT mastery FROM topics WHERE id=?", (tid_kokugo,)).fetchone()["mastery"]
    # 国語: ratio=0.5, α=0.4, 初期0 → 0.2
    assert abs(m_kokugo - 0.2) < 1e-9
    ok(f"mastery: 国語={m_kokugo:.3f} (期待0.200), 算数={m_math:.3f} (誤答で下降)")
except Exception as e:
    ng("apply_grading_results", str(e))


# ─────────────────────────────────────────────
print("\n=== 12. P6: 集計ヘルパー (get_weak_points / mastery_trend / teaching_notes) ===")
try:
    # 既存の TEST_USER / ROUTE_USER のデータを利用。ROUTE_USER は
    # 算数(tid_math)に誤答2件、国語(tid_kokugo)に誤答1件が登録されている想定。
    weak = bot.get_weak_points(ROUTE_USER, days=7, limit=3)
    assert isinstance(weak, list) and len(weak) >= 1
    # グループ化されていること
    subjects_in_weak = {w["subject"] for w in weak}
    assert "算数" in subjects_in_weak or "国語" in subjects_in_weak
    # 例が載っていること
    assert all(("count" in w and w["count"] >= 1) for w in weak)
    assert any(w.get("example") and w["example"].get("teaching_note") for w in weak)
    ok(f"get_weak_points: {len(weak)}グループ, 教科={subjects_in_weak}")

    trend = bot.get_mastery_trend(ROUTE_USER, days=7)
    assert isinstance(trend, list) and len(trend) >= 1
    for t in trend:
        assert "current_mastery" in t
        assert "recent_ratio" in t
        assert "prior_ratio" in t
        assert "delta" in t
    ok(f"get_mastery_trend: {len(trend)}単元の推移を計算")

    notes = bot.get_recent_teaching_notes(ROUTE_USER, days=7, limit=10)
    assert isinstance(notes, list) and len(notes) >= 1
    assert all(n.get("teaching_note") for n in notes)
    # 誤答優先の並び（先頭の is_correct は 0 のはず）
    assert notes[0]["is_correct"] == 0
    ok(f"get_recent_teaching_notes: {len(notes)}件 (誤答優先)")

    # フォーマット関数も基本的な出力を返すこと
    wb = bot.format_weak_points_block(weak)
    tb = bot.format_mastery_trend_block(trend)
    assert "誤答" in wb and "定着度" in tb
    ok("format_weak_points_block / format_mastery_trend_block: 出力OK")
except Exception as e:
    ng("P6 集計", str(e))


# ─────────────────────────────────────────────
print("\n=== 13. P6: 日次レポート生成（Claude呼び出し） ===")
print("  (Claude APIを呼び出し中...)")
try:
    # 独立性のため、本日の learning_records を全て status='done' に矯正
    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            """UPDATE learning_records SET status='done',
               score=COALESCE(score, total)
               WHERE user_id=? AND status<>'done'""",
            (TEST_USER,),
        )
    today = _dt.date.today().isoformat()
    records = bot.get_daily_summary(TEST_USER, today)
    assert len(records) >= 1, f"本日の学習記録がない: {records}"
    balance = bot.get_credits(TEST_USER)
    report = bot.generate_daily_report(records, balance, user_id=TEST_USER)
    assert isinstance(report, str) and len(report) > 50, f"レポート短すぎ: {report!r}"
    ok(f"日次レポート生成: {len(report)}文字")
    print("    ----- daily report -----")
    for line in report.splitlines():
        print(f"    {line}")
    print("    ------------------------")
except Exception as e:
    ng("日次レポート", str(e))


print("\n=== 14. P6: 週次レポート生成（Claude呼び出し） ===")
print("  (Claude APIを呼び出し中...)")
try:
    # ROUTE_USER の lr_math / lr_kokugo を完了させる（status='done'）
    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            """UPDATE learning_records SET status='done',
               score=COALESCE(score, 0), total=COALESCE(NULLIF(total,0), 2)
               WHERE user_id=? AND status<>'done'""",
            (ROUTE_USER,),
        )
    records = bot.get_weekly_summary(ROUTE_USER)
    assert len(records) >= 1, f"今週の学習記録がない: {records}"
    balance = bot.get_credits(ROUTE_USER)
    report = bot.generate_weekly_report(records, balance, user_id=ROUTE_USER)
    assert isinstance(report, str) and len(report) > 100, f"レポート短すぎ: {report!r}"
    # 見出しのうち少なくとも1つは含む想定（絵文字/キーワード）
    assert any(kw in report for kw in ("成長", "補強", "指導", "来週")), \
        f"家庭教師視点キーワードが欠落: {report[:200]}"
    ok(f"週次レポート生成: {len(report)}文字, 家庭教師視点の見出し含む")
    print("    ----- weekly report -----")
    for line in report.splitlines():
        print(f"    {line}")
    print("    -------------------------")
except Exception as e:
    ng("週次レポート", str(e))


# ─────────────────────────────────────────────
print("\n=== P1-2. 交換承認のトランザクション整合性 ===")
try:
    P12_USER = "U_p12_child"
    with sqlite3.connect(TEST_DB) as c:
        c.execute("DELETE FROM exchanges WHERE user_id=?", (P12_USER,))
        c.execute("DELETE FROM credits WHERE user_id=?", (P12_USER,))
        c.execute("INSERT INTO credits (user_id, balance) VALUES (?, ?)", (P12_USER, 100))
        c.execute("INSERT INTO exchanges (user_id, prize_name, cost, status) VALUES (?, ?, ?, 'pending')",
                  (P12_USER, "P12景品A", 50))
        c.execute("INSERT INTO exchanges (user_id, prize_name, cost, status) VALUES (?, ?, ?, 'pending')",
                  (P12_USER, "P12景品B", 80))
        ex_a_id = c.execute("SELECT id FROM exchanges WHERE user_id=? AND prize_name='P12景品A'",
                            (P12_USER,)).fetchone()[0]
        ex_b_id = c.execute("SELECT id FROM exchanges WHERE user_id=? AND prize_name='P12景品B'",
                            (P12_USER,)).fetchone()[0]

    # ケース1: 承認を連続2回 → 2回目は None、残高は 1回分のみ差引
    r1 = bot.approve_exchange_if_pending(ex_a_id, P12_USER)
    assert r1 is not None and r1["status"] == "approved", f"1回目承認失敗: {r1}"
    assert r1["new_balance"] == 50, f"残高不正: {r1['new_balance']}"
    r1_dup = bot.approve_exchange_if_pending(ex_a_id, P12_USER)
    assert r1_dup is None, f"二重承認が通ってしまった: {r1_dup}"
    with sqlite3.connect(TEST_DB) as c:
        bal = c.execute("SELECT balance FROM credits WHERE user_id=?", (P12_USER,)).fetchone()[0]
    assert bal == 50, f"二重承認でクレジットが二重差引された: balance={bal}"
    ok("二重承認で差引は 1 回のみ")

    # ケース2: 残高不足 → pending のまま、reason=insufficient_balance
    r2 = bot.approve_exchange_if_pending(ex_b_id, P12_USER)
    assert r2 is not None, "残高不足時に None 返却 (区別できない)"
    assert r2.get("reason") == "insufficient_balance", f"reason不正: {r2}"
    assert r2["balance"] == 50 and r2["cost"] == 80
    with sqlite3.connect(TEST_DB) as c:
        status = c.execute("SELECT status FROM exchanges WHERE id=?", (ex_b_id,)).fetchone()[0]
        bal = c.execute("SELECT balance FROM credits WHERE user_id=?", (P12_USER,)).fetchone()[0]
    assert status == "pending", f"残高不足なのに {status} に遷移"
    assert bal == 50, f"残高不足時に balance が変わった: {bal}"
    ok("残高不足は pending のまま、balance 不変")

    # ケース3: finalize_grading の原子性 — 途中で RuntimeError を起こす
    #   apply_grading_results 成功後、update_topic_mastery を monkeypatch で失敗させ、
    #   learning_records.status / credits.balance の更新がすべて巻き戻ることを確認
    with sqlite3.connect(TEST_DB) as c:
        c.execute("INSERT INTO credits (user_id, balance) VALUES (?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance",
                  ("U_p12_atomic", 0))
        sid = c.execute(
            "INSERT INTO sessions (user_id) VALUES (?)", ("U_p12_atomic",),
        ).lastrowid
        tid = c.execute(
            "INSERT INTO topics (subject, unit) VALUES ('算数', 'atomic_test')"
        ).lastrowid
        lr_id = c.execute(
            "INSERT INTO learning_records (session_id, topic_id, user_id, status) "
            "VALUES (?, ?, ?, 'grading')",
            (sid, tid, "U_p12_atomic"),
        ).lastrowid

    orig = bot.update_topic_mastery
    def boom(*a, **kw):
        raise RuntimeError("simulated mid-transaction failure")
    bot.update_topic_mastery = boom
    try:
        raised = False
        try:
            bot.finalize_grading(
                user_id="U_p12_atomic", default_topic_id=tid, learning_record_id=lr_id,
                questions=[{"q": "1+1", "a": "2", "type": "calc", "origin": "today"}],
                results=[{"correct": True, "student_answer": "2"}],
                credit_per_correct=5,
            )
        except RuntimeError:
            raised = True
        assert raised, "RuntimeError が伝播しなかった"
    finally:
        bot.update_topic_mastery = orig

    with sqlite3.connect(TEST_DB) as c:
        lr_status = c.execute("SELECT status FROM learning_records WHERE id=?", (lr_id,)).fetchone()[0]
        bal = c.execute("SELECT balance FROM credits WHERE user_id=?", ("U_p12_atomic",)).fetchone()[0]
        qa_n = c.execute("SELECT COUNT(*) FROM question_attempts WHERE learning_record_id=?",
                         (lr_id,)).fetchone()[0]
    assert lr_status == "grading", f"learning_record が done に遷移してしまった: {lr_status}"
    assert bal == 0, f"途中失敗でもクレジットが加算された: {bal}"
    assert qa_n == 0, f"途中失敗でも question_attempts が残った: {qa_n}"
    ok("finalize_grading 途中失敗で全変更がロールバック")

except Exception as e:
    import traceback
    traceback.print_exc()
    ng("P1-2 トランザクション整合性", str(e))


# ─────────────────────────────────────────────
print("\n=== P2-3. daily_reports キャッシュ ===")
try:
    from datetime import date as _date
    P23_USER = "U_p23_child"
    today = _date.today().isoformat()
    records = [{
        "subject_name": "算数", "unit": "分数のかけ算",
        "score": 2, "total": 3, "summary": "約分忘れ",
    }]

    # Claude モックでコール数を数える
    class _FakeText:
        def __init__(self, t):
            self.type = "text"
            self.text = t
    class _FakeMsg:
        def __init__(self, t): self.content = [_FakeText(t)]
    calls = {"n": 0}
    class _FakeMessages:
        def create(self, **kw):
            calls["n"] += 1
            return _FakeMsg(f"mock-report-{calls['n']}")
    class _FakeClaude:
        messages = _FakeMessages()
    orig_claude = bot.claude
    bot.claude = _FakeClaude()

    # 既存キャッシュをクリア
    with sqlite3.connect(TEST_DB) as c:
        c.execute("DELETE FROM daily_reports WHERE user_id=?", (P23_USER,))

    try:
        # 1回目: Claude 呼び出しが発生し、キャッシュに保存される
        r1 = bot.generate_daily_report(records, balance=50, user_id=P23_USER)
        assert calls["n"] == 1, f"初回で Claude が呼ばれていない: {calls}"
        assert r1 == "mock-report-1"
        with sqlite3.connect(TEST_DB) as c:
            n_cache = c.execute(
                "SELECT COUNT(*) FROM daily_reports WHERE user_id=? AND day=?",
                (P23_USER, today),
            ).fetchone()[0]
        assert n_cache == 1, f"キャッシュ未保存: {n_cache}"
        ok("初回で Claude 呼び出し + キャッシュ保存")

        # 2回目: 同一入力 → キャッシュヒット (Claude 呼ばれない)
        r2 = bot.generate_daily_report(records, balance=50, user_id=P23_USER)
        assert calls["n"] == 1, f"同一入力でも Claude が再度呼ばれた: {calls}"
        assert r2 == r1
        ok("同一入力でキャッシュヒット (Claude 呼び出し回避)")

        # 3回目: balance 変化 → プロンプト差分 → キャッシュミス
        r3 = bot.generate_daily_report(records, balance=60, user_id=P23_USER)
        assert calls["n"] == 2, f"入力差分時に Claude が呼ばれなかった: {calls}"
        assert r3 == "mock-report-2"
        ok("入力差分でキャッシュミス (再生成)")

        # 4回目: user_id=None → キャッシュ非利用 (常に Claude を呼ぶ)
        r4 = bot.generate_daily_report(records, balance=50, user_id=None)
        assert calls["n"] == 3, f"user_id=None でもキャッシュに乗った: {calls}"
        ok("user_id=None はキャッシュ対象外")
    finally:
        bot.claude = orig_claude
        with sqlite3.connect(TEST_DB) as c:
            c.execute("DELETE FROM daily_reports WHERE user_id=?", (P23_USER,))

except Exception as e:
    import traceback
    traceback.print_exc()
    ng("P2-3 daily_reports キャッシュ", str(e))


# ─────────────────────────────────────────────
print("\n=== P2-1. schema_migrations ランナー ===")
try:
    import tempfile
    # ケース1: 新規 DB → 0001 が記録される
    _saved = bot.DB_PATH
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        fresh = tf.name
    try:
        bot.DB_PATH = fresh
        applied_1 = bot.run_migrations()
        assert 1 in applied_1, f"新規 DB で 0001 が適用されていない: {applied_1}"
        with sqlite3.connect(fresh) as c:
            rows = sorted(c.execute("SELECT version, name FROM schema_migrations"))
        names = [n for _, n in rows]
        assert (1, "0001_init.sql") in rows, f"schema_migrations に 0001 なし: {rows}"
        assert all(n.endswith(".sql") for n in names), f"名称不正: {names}"
        applied_2 = bot.run_migrations()
        assert applied_2 == [], f"2 回目に再適用されてしまった: {applied_2}"
        ok("新規 DB でランナーが各 migration を 1 回だけ適用")
    finally:
        bot.DB_PATH = _saved
        try:
            os.remove(fresh)
        except OSError:
            pass

    # ケース2: レガシー DB（旧 init_db 相当でテーブルだけ存在、schema_migrations 不在）
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        legacy = tf.name
    try:
        with sqlite3.connect(legacy) as c:
            c.execute("CREATE TABLE credits (user_id TEXT PRIMARY KEY, balance INTEGER DEFAULT 0)")
            c.execute("INSERT INTO credits (user_id, balance) VALUES ('legacy_user', 42)")
        bot.DB_PATH = legacy
        applied_legacy = bot.run_migrations()
        assert 1 in applied_legacy, f"レガシー DB で 0001 が登録されなかった: {applied_legacy}"
        with sqlite3.connect(legacy) as c:
            bal = c.execute("SELECT balance FROM credits WHERE user_id='legacy_user'").fetchone()[0]
            sm = sorted(r[0] for r in c.execute("SELECT version FROM schema_migrations"))
        assert bal == 42, f"レガシーデータが破壊された: {bal}"
        assert 1 in sm, f"schema_migrations 不正: {sm}"
        ok("レガシー DB もデータを壊さず全 migration を登録")
    finally:
        bot.DB_PATH = _saved
        try:
            os.remove(legacy)
        except OSError:
            pass

except Exception as e:
    import traceback
    traceback.print_exc()
    ng("P2-1 schema_migrations ランナー", str(e))


# ─────────────────────────────────────────────
print(f"\n{'='*40}")
print(f"結果: {passed}件成功 / {failed}件失敗")
if failed == 0:
    print("✅ 全テスト通過 — Botは正常に動作します")
else:
    print("❌ 失敗があります。上記エラーを確認してください")
print('='*40)

# クリーンアップ
if failed == 0 and os.path.exists(TEST_DB):
    os.remove(TEST_DB)
