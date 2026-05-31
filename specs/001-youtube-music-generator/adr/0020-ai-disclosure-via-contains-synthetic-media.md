# ADR-0020: AI 開示フラグ = `status.containsSyntheticMedia=true`(YouTube Data API)

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** ml, policy, ops, backend

## 背景

要件「AI生成コンテンツの開示フラグ (Altered content) を投稿時に付与」の API 実装方式が未確定だった。
当初は「YouTube Studio UI で手動設定が必要か?」という懸念があったが、調査で API 完結が可能と判明。

事実関係(調査済み):

- YouTube Data API v3 に **`status.containsSyntheticMedia`**(boolean)が公式サポートされている
- `videos.insert` および `videos.update` で設定可能
- API レスポンスにも含まれる
- YouTube ポリシー(2024 開始、2025-05 拡張、2026 で完全施行)に準拠
- 開示が必要なケース: 現実的な人物の合成・現実イベントの改変・現実的なシーン生成
- YouTube 側で未開示の AI コンテンツを検出した場合、自動でラベルが付与され、クリエイターは削除不可

## 決定

- すべての動画投稿時に `status.containsSyntheticMedia=true` を必ず設定
- google-api-python-client 経由で `videos.insert` を呼び出す際の body は以下の構造:

```python
body = {
    "snippet": {
        "title": "...",
        "description": "...",
        "categoryId": "10",  # Music
        "tags": [...],
    },
    "status": {
        "privacyStatus": "public",
        "containsSyntheticMedia": True,  # AI 開示フラグ(必須)
        "selfDeclaredMadeForKids": False,
    },
}
request = youtube.videos().insert(
    part="snippet,status",
    body=body,
    media_body=MediaFileUpload("video.mp4", chunksize=-1, resumable=True),
)
response = request.execute()
```

- 投稿直前のバリデーション層で `containsSyntheticMedia=true` が body に含まれているか **必ず確認**(`true` でない場合は投稿を停止して Slack 通知)
- バリデーションは単体テストでも明示的に網羅(コード変更で誤って外れることを防ぐ)
- dryrun モード(ADR-0007)でも body 生成までは行うため、バリデーションは dryrun でも動作する

## 結果

### 良い影響

- API 完結で自動化が成立、UI 手動操作は不要
- バリデーション層で抜け漏れを機械的に検知できる
- YouTube ポリシー違反による事後ラベル付与(クリエイターが削除不可)を回避

### 悪い影響・トレードオフ

- `containsSyntheticMedia=true` の表示は YouTube 視聴者にも見える("Altered content" ラベル)
  - 受容: ポリシー遵守は前提、隠す選択肢を取らない

### 受容したリスク

- 将来の API 仕様変更でフィールド名や挙動が変わる可能性
  - 対策: google-api-python-client のバージョン固定、リリースノート追従
- YouTube が将来「音楽の場合は不要」等の例外を設けても、保守的に常時 `true` で運用

## 検討した代替案

- **投稿後 Studio UI で手動設定:** 人手介入が必要で自動化が破綻、忘れリスクで BAN 加速。不採用。
- **未開示で投稿、YouTube の自動検知に任せる:** ポリシー違反、検知後の事後ラベルは削除不可、評価毀損。不採用。
- **音楽ジャンルのみ未設定:** YouTube が将来例外を明文化するまで保守的に `true`。当面不採用。

## 関連

- ADR-0004: 投稿規模(BAN リスク管理)
- ADR-0005: Content ID 事前チェック(コンプライアンス系列)
- ADR-0007: dryrun モード(バリデーション動作)
- ../requirements.md §YouTube運用方針
- YouTube Data API v3 `videos.insert`: <https://developers.google.com/youtube/v3/docs/videos/insert>
- YouTube AI 開示ポリシー: <https://support.google.com/youtube/answer/14328491>
