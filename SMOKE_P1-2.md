# P1-2 スモークテスト手順書

P1-2 (クレジット・採点のトランザクション化 + 交換承認のアプリ側残高ガード) を supervisor モード経由で実機検証する。

## 前提
- `.env` に `SUPERVISOR_USER_ID=<自分のLINE ID>` が設定済
- `supervisor_profile.json` 作成済
- bot 起動中 (`python bot.py`) で ngrok 経由 or 直接 LINE webhook に接続可

> すべて **supervisor-child (`sv-child:<LINE_ID>`)** 名義のデータなので、本番の息子データには一切影響しません。

---

## 1. supervisor モード確認
1. `/sv status` を送信
2. 期待: 現在のモード (parent or child)、supervisor-child の学習記録数・残高などが返る

---

## 2. 正常系 — finalize_grading トランザクション
**目的:** 画像分析 → 出題 → 採点 → クレジット加算が 1 トランザクションで整合することを確認

1. `/sv child` でモードを child に
2. 自由テキスト「算数」送信 → Bot が画像リクエスト
3. 手書きノートの写真を 1〜2 枚送信 → 「採点待ちです」的な応答
4. `/sv parent` でモードを parent に
5. 子の回答画像 (数値や解答) を送信 → 採点結果が返る
6. 期待: `採点: X/Y 正解 / 獲得: Ncr / 残高: Mcr` が表示され、同じ数字が `/sv status` にも反映

**check:** 途中で通信切断などの失敗があっても、部分適用 (score だけ更新されたのに credits が増えていない、等) が無いこと。

---

## 3. 承認の二重送信耐性
**目的:** `/承認` を 2 回送っても二重差引が起きない

1. `/sv child` で景品申請 — 残高で足りる cost の景品を選ぶ
2. `/sv parent` で `一覧` → `承認 <id>`
3. 期待: 「承認しました (差引: Ncr、残高: Mcr)」
4. すぐ同じ `承認 <id>` を再送
5. 期待: 「申請 #X は既に処理済みか存在しません。」
6. `/sv status` で残高が **1回分しか引かれていない** ことを確認

---

## 4. 残高不足ガード
**目的:** cost > balance の景品を承認しようとしたとき、pending のまま「残高不足」メッセージが出る

準備: テスト DB を直接いじる方が早い。bot を止めずに別シェルから:

```bash
cd /c/Users/tsuch/dev/study-bot
PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -c "
import sqlite3
conn = sqlite3.connect('study_bot.db')
# supervisor-child の残高を 10 にする
sv_child = 'sv-child:<あなたのLINE_ID>'
conn.execute('INSERT INTO credits (user_id, balance) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance', (sv_child, 10))
# cost=999 の景品申請を pending で追加
conn.execute(\"INSERT INTO exchanges (user_id, prize_name, cost, status) VALUES (?, '残高不足テスト', 999, 'pending')\", (sv_child,))
conn.commit()
print('exchange id=', conn.execute(\"SELECT id FROM exchanges WHERE prize_name='残高不足テスト'\").fetchone()[0])
"
```

1. `/sv parent` で `一覧`
2. `承認 <上記id>` を送信
3. **期待:**
   - 「申請 #X「残高不足テスト」は残高不足のため承認できません。(必要: 999cr / 現在残高: 10cr) 申請は pending のまま残っています。」
   - `/sv status` で残高は **10 のまま** (変化なし)
   - DB で `exchanges.status='pending'` のまま

---

## 5. 後片付け
1. `/sv reset`
2. `/sv reset confirm`
3. 期待: supervisor-child 系のすべての行が削除された旨のメッセージ

---

## 合格条件 (すべて満たすこと)
- [ ] §2: 採点結果・クレジット・残高が一致
- [ ] §3: 2 回目の承認が「既に処理済み」応答、差引は 1 回のみ
- [ ] §4: 残高不足承認が「残高不足」メッセージで拒否され、balance/status 不変
- [ ] §5: /sv reset confirm で sv-child:* の行が消える

## NG 時のロールバック
- §3/§4 の挙動が期待と異なる: `git revert <P1-2 関連コミット>` (77cb536 / 29ed063 より前)
- supervisor-child データが残る: `/sv reset confirm` を再実行
