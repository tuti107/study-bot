# StudyBot 家庭教師AI仕様書

## 0. 目的とコンセプト

本システムの核心は「**写真を見てテストを作る機械**」ではなく「**優秀な家庭教師**」である。
目指すべき振る舞い：

1. 今日の学習内容から、その**単元・難易度・つまずきポイント**を的確に見抜く
2. **少ない問題数で最大の知識定着**を図る（丸暗記ではなく理解の確認）
3. **過去の学習履歴を踏まえた**出題（忘却曲線に沿った復習、関連単元との橋渡し）
4. 間違えた箇所を**弱点として記録**し、将来の出題に反映する

---

## 1. 学習者プロファイル（新規）

家庭教師が指導する相手を知らないと良い指導はできない。固定の前提情報を管理する。

### 1.1 ファイル: `student_profile.json`

```json
{
  "name": "○○",
  "grade": 4,
  "school_type": "公立",
  "subjects": ["算数", "国語", "理科", "社会", "英語"],
  "textbooks": {
    "算数": "東京書籍 新編 新しい算数 4年",
    "国語": "光村図書 国語 四年"
  },
  "strengths": ["計算", "音読"],
  "weaknesses": ["文章題", "漢字の書き"],
  "notes": "集中力は短め。1回10〜15分が適切。"
}
```

### 1.2 用途

- すべてのClaude呼び出しでシステムコンテキストとして注入
- 学年が分かることで、未習範囲の出題を回避できる
- `weaknesses` は時間と共に更新される動的フィールド（後述）

---

## 2. データモデル刷新

### 2.1 現行の問題点

| 現行テーブル | 問題 |
|---|---|
| `subjects` | 1セッション内で閉じており、単元として抽象化されていない。過去との関連が切れる |
| `session_images` | 画像パスのみ。Claudeが画像から抽出した情報が破棄されている |
| 履歴の粒度 | 「科目」止まり。単元（例：分数の足し算）や概念（例：通分）のレベルで追跡できない |

### 2.2 新テーブル構成

#### `topics`（単元マスター：動的に成長）
学習した単元を概念レベルで正規化・再利用するための軸。

| 列 | 型 | 説明 |
|---|---|---|
| id | INTEGER PK | |
| subject | TEXT | 算数、国語など |
| unit | TEXT | 「分数の足し算」「漢字の音読み」など単元名 |
| concept_keys | TEXT (JSON配列) | ["通分", "約分", "異分母"] 概念タグ |
| grade_introduced | INTEGER | 初出学年 |
| first_seen_at | DATETIME | このトピックで最初に学習した日 |
| last_seen_at | DATETIME | 最後に学習した日 |
| mastery | REAL | 0.0〜1.0の定着度（累積正答率の加重平均） |

#### `learning_records`（学習1回分）
旧 `subjects` の発展形。1セッションの1科目 = 1レコード。

| 列 | 型 | 説明 |
|---|---|---|
| id | INTEGER PK | |
| session_id | INTEGER | |
| topic_id | INTEGER FK | `topics.id` |
| user_id | TEXT | |
| learned_at | DATE | |
| source_summary | TEXT | Claudeが画像から抽出した「今日学んだことの要約」 |
| detected_difficulty | TEXT | easy / standard / challenging |
| stumble_points | TEXT (JSON配列) | Claudeが予測した「小学生がつまずきやすいポイント」 |
| questions | TEXT (JSON) | 出題内容 |
| total | INTEGER | |
| score | INTEGER | |
| status | TEXT | waiting / done |

#### `question_attempts`（問題単位の結果）
今まで失われていた「どの問題で間違えたか」を残す。復習の燃料。

| 列 | 型 | 説明 |
|---|---|---|
| id | INTEGER PK | |
| learning_record_id | INTEGER FK | |
| topic_id | INTEGER FK | |
| question_text | TEXT | |
| correct_answer | TEXT | |
| student_answer | TEXT | |
| is_correct | BOOLEAN | |
| question_type | TEXT | knowledge / application / reasoning |
| concept_keys | TEXT (JSON) | その問題が問うている概念タグ |
| mistake_category | TEXT | 誤答時のみ：計算ミス / 概念誤解 / 未学習 / 読み違い など |
| attempted_at | DATETIME | |

#### `review_queue`（復習キュー：spaced repetition）
エビングハウス忘却曲線に基づく復習対象管理。

| 列 | 型 | 説明 |
|---|---|---|
| id | INTEGER PK | |
| user_id | TEXT | |
| topic_id | INTEGER FK | |
| concept_key | TEXT | トピック内の特定概念を狙い撃ちする場合 |
| reason | TEXT | `mistake` / `scheduled_review` / `weak_point` |
| scheduled_for | DATE | 次回出題予定日 |
| interval_days | INTEGER | 次回までの間隔（1→3→7→14→30） |
| times_reviewed | INTEGER | |
| last_result | TEXT | correct / incorrect / pending |
| status | TEXT | pending / done / retired |

**間隔スケジュール方針**：
- 誤答: 1日後
- 正答1回: 3日後 → 7日後 → 14日後 → 30日後 → 卒業
- 復習で再誤答: 間隔を1日にリセット

---

## 3. プロンプト設計

### 3.1 共通システムコンテキスト

全Claude呼び出しに以下を注入する。

```
あなたは経験豊富な家庭教師です。指導相手は以下の小学生です。

【学習者】
- 名前: {{name}}
- 学年: 小学{{grade}}年生
- 使用教科書: {{textbooks}}
- 得意: {{strengths}}
- 苦手: {{weaknesses}}
- 備考: {{notes}}

【指導方針】
- 学年の既習範囲のみから出題し、未習の概念は問わない
- 丸暗記ではなく理解を問う（「なぜそうなるか」「どう使うか」を重視）
- 苦手分野は丁寧に、得意分野はチャレンジを入れる
- 短時間集中が前提。問題数は少なめ（3〜5問）で質を重視
```

### 3.2 画像分析・小テスト生成プロンプト（改訂）

現行の「問題3問作って」を、**2段階推論**に分解する。

#### ステップA（理解）：画像から学習内容を構造化

このステップでは **Web検索ツール（Anthropic API の web_search tool）を有効化**し、Claudeに一般的な指導知見を調べさせる。学習者固有の傾向（プロファイル）と、**その単元で全国的に観察される典型的なつまずき**（教育サイト、教師ブログ、塾の解説記事、学習指導要領解説等）の双方を参照させる。

```
【タスク】
添付画像は本日の学習ページ（教科書またはノート）です。以下を分析してください。

【プロセス】
1. 画像から単元を推定する
2. その単元について、必要に応じて web_search ツールで
   「小学{{grade}}年 {{推定単元}} つまずき よくある間違い」
   「{{推定単元}} 指導 ポイント 誤概念」等を検索し、
   一般的に小学生がつまずきやすいポイントを調査する
3. 学習者固有の傾向（苦手: {{weaknesses}}、備考: {{notes}}）と
   一般的傾向の双方を統合して stumble_points を作成する

【過去の学習履歴】（直近30日）
{{recent_topics_summary}}
例: 2026-04-10 算数「整数の割り算」定着度0.6 | 2026-04-12 国語「漢字(4年前期)」定着度0.4

【現在の復習候補】
{{due_reviews}}
例: 算数「分数の足し算」概念「通分」(誤答から5日経過)

【出力形式】分析が終わったらJSONのみを返してください。
{
  "subjects": [
    {
      "subject_name": "算数",
      "unit_guess": "分数の足し算（異分母）",
      "concept_keys": ["通分", "約分", "最小公倍数"],
      "source_summary": "教科書p.42-43。異分母分数の加法を通分により計算する手順を学習。",
      "difficulty": "standard",
      "stumble_points": [
        {"point": "通分する際に分母の最小公倍数を取り違える", "source": "general"},
        {"point": "答えが仮分数になったときに帯分数への変換を忘れる", "source": "general"},
        {"point": "約分の見落とし（この子は特に計算の最後が雑になりがち）", "source": "profile"}
      ],
      "research_notes": "web検索で確認した主要な指導上の注意点の要約（1〜2文）",
      "links_to_past": [
        {"topic": "分数の意味", "relation": "前提知識"},
        {"topic": "最小公倍数", "relation": "2週間前に学習。使うのは初めて"}
      ]
    }
  ]
}
```

**web_search の使い方**：
- Anthropic API の `tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]` を指定
- 1リクエストあたり最大3回の検索に制限（コスト・レイテンシ対策）
- 検索が不要と判断された場合（過去に同じ単元を分析済み等）はスキップ可能

**キャッシュ戦略**（コスト対策）：
- `topic_research_cache` テーブルを別途追加し、単元×概念キーの組で研究結果を保存
- 同じ単元の2回目以降は検索を省略して過去結果を再利用（`source="cache"`）

#### ステップB（出題）：分析結果＋復習キューから問題を生成

ステップAの結果と `review_queue` を入力として、以下のロジックで出題する。

```
【タスク】
以下の学習分析と復習候補から、本日の小テストを作ってください。

【本日の学習】
{{step_a_output}}

【復習すべき項目（過去の誤答・スケジュール復習）】
{{due_reviews_detail}}
例: 
- 算数「分数の足し算」概念「通分」(3日前に誤答)
  誤答内容: 1/2 + 1/3 = 2/5 と答えた

【出題方針】
1. 本日の学習内容から3問（stumble_pointsの1つ以上を意図的に問う）
   - 最低1問は「なぜそうなるか」「どういうときに使うか」を問う応用・推論問題
   - 最低1問は基礎の確認問題
2. 復習候補から最大2問（過去誤答を優先、なければスケジュール分）
   - 誤答した問題そのものではなく、同じ概念を問う別の問題にする
3. 全問、小学{{grade}}年生が答えられる言葉で

【出力形式】JSON配列。
[
  {
    "q": "問題文",
    "a": "正解",
    "type": "knowledge | application | reasoning",
    "concept_keys": ["通分"],
    "origin": "today | review",
    "review_topic_id": null または対応するtopic_id,
    "intent": "この問題で何を確認したいか（例：通分の仕組みを理解しているか）"
  }
]
```

### 3.3 採点プロンプト（改訂）

単純な○×ではなく、**誤答の種類**を分類させる。これが弱点DB更新の入力になる。

```
【タスク】
小学{{grade}}年生の答案を採点してください。

【採点方針】
- 意味が合っていれば正解（表記ゆれは許容）
- 誤答の場合は原因を分類してください：
  - calc_error: 計算ミス（方針は合っている）
  - concept_error: 概念を誤解している
  - read_error: 問題の読み違い
  - unknown: 未学習または白紙
  - partial: 部分的には合っている

【出題情報】
{{questions_with_intent_and_concepts}}

【出力形式】
[
  {
    "q": "問題文",
    "student_answer": "生徒の答え",
    "correct": true/false,
    "mistake_category": "calc_error 等（正解時はnull）",
    "concept_keys": ["通分"],
    "comment": "小学生向けに優しく具体的な一言（誤答時は正しい考え方のヒント）",
    "teaching_note": "保護者向け：何が分かっていて何が分かっていないか"
  }
]
```

### 3.4 日次・週次レポートプロンプト（改訂）

現行は当日の正答数を伝えるだけ。以下を追加する：

- 単元別の定着度推移（前回比）
- 今週の弱点トップ3とその具体例
- 家庭教師としての具体的な提案（「明日は通分を絵で見せてあげてください」等）
- 褒めポイント（伸びた単元、継続できていること）

---

## 4. RAG的な過去情報の組み込み

「RAG」というほど大げさではなく、**プロンプトへの文脈注入**で十分な規模。

### 4.1 注入する情報の作り方

各Claude呼び出し前に以下をDB集計してプロンプトに埋める：

| 情報 | クエリ | 用途 |
|---|---|---|
| 直近30日の学習トピック＋定着度 | `topics` を `last_seen_at DESC` | 関連付け・重複回避 |
| 復習予定（本日以前に `scheduled_for`） | `review_queue WHERE scheduled_for <= today AND status='pending'` | 今日のテストに混ぜる |
| 最近の誤答（過去14日） | `question_attempts WHERE is_correct=0` | 弱点パターン把握 |
| 定着度の低いトピック | `topics WHERE mastery < 0.6` | 要補強の可視化 |

### 4.2 トークン量の管理

- 直近30日でも件数が膨らむので、**要約済みの `source_summary` と `mastery` のみ**を渡す
- 個別問題文までは渡さない（それが必要なら別途検索で取り出す）
- 想定サイズ: プロフィール 200トークン + 履歴サマリ 500トークン + 復習キュー 300トークン = 約1000トークン追加

---

## 5. 忘却曲線に基づく復習ロジック

### 5.1 トリガー

- 採点完了時に `question_attempts` が書かれる
- その裏で以下を自動実行：

```
for attempt in attempts:
  if attempt.is_correct == False:
    review_queue に INSERT (reason='mistake', scheduled_for=today+1日)
  else:
    既存のreview_queueエントリを更新
    - times_reviewed++
    - interval_days を次段階に（1→3→7→14→30）
    - scheduled_for = today + interval_days
    - 30日を超えたら status='retired'（卒業）

topics.mastery = 移動平均で更新（新しい結果ほど重み大）
```

### 5.2 出題時の混ぜ方

- 本日の新規出題: 3問
- 復習問題: 最大2問（今日が `scheduled_for <= today` のうち古い順）
- 復習問題は「前回と完全同一の問題」ではなく、**同じ概念の別問題**をClaudeに作らせる

---

## 6. 段階的移行計画

破壊的変更なので一気に入れると動かなくなる。順に：

| フェーズ | 内容 | 所要 |
|---|---|---|
| **P1** | `student_profile.json` 導入。現行プロンプトに学年・苦手を注入するだけ | 小 |
| **P2** | `topics`, `learning_records`, `question_attempts` テーブル追加と既存フローの置き換え（`review_queue` はまだ使わず書き込みのみ） | 中 |
| **P3** | ステップA/B分割プロンプト＋ `source_summary`, `stumble_points`, `intent` を実装 | 中 |
| **P4** | `review_queue` の更新ロジック実装。ただし出題には未反映 | 小 |
| **P5** | 復習問題の出題（ステップBに復習候補を渡す） | 中 |
| **P6** | レポート改訂（単元別定着度、提案コメント） | 小 |

各フェーズ後に `test_bot.py` を更新して回帰を確認する。

---

## 7. 確定事項（利用者判断済み）

1. **学年・教科書情報の入力方法**: 案A（`student_profile.json` を手動編集）
2. **採点コメントの厳しさ**: ヒントのみ、答えは示唆しない
3. **復習問題の表示**: 「今日のテスト」「おさらい」とセクション分け
4. **定着度スコア計算式**: 指数移動平均 α=0.4 で開始
5. **教科書**: 以下で固定登録
   - 算数: 小学算数6（教育出版）
   - 国語: 国語六 創造（光村図書）
   - 理科: 新編新しい理科6（東京書籍）
   - 社会: 小学社会6（教育出版）
6. **学習者プロファイル**: `student_profile.json` に登録済み（§1.1参照）

---

## 8. 完了定義（このシステムが「良い家庭教師」と言える条件）

- [ ] 同じ単元を繰り返し誤答した場合、自動的に復習問題が生成される
- [ ] 学習履歴が増えると、今日のテストに過去の関連単元が絡んで出題される
- [ ] 週次レポートで「今週の成長」「要補強の単元」「来週の方針」が具体的に述べられる
- [ ] 出題はすべて学年の既習範囲に収まり、未習概念が問われない
- [ ] 誤答の分類（計算ミス / 概念誤解 / 読み違い）が記録され、保護者が傾向を把握できる
