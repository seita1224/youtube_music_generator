# ADR-0013: 管理UI 認証 = frontend セッション + backend Basic(LAN 内)

- **ステータス:** Accepted (Updated)
- **日付:** 2026-05-25 (更新: 2026-07-13)
- **決定者:** @seita
- **タグ:** security, frontend, backend, ops

## 背景

管理UI からは LLM プロンプト差し替え・dryrun 承認・動画投稿管理など、**システム全体に影響する操作**が可能。
無認証で LAN に晒すと、第三者が乗っ取って BAN リスクのある投稿を実行できる。

想定運用:

- 1人運用
- GPU マシンは家庭内 LAN に常設
- 操作は GPU マシン本体だけでなく **同 LAN 内の別 PC・タブレット** からも行いたい

初期実装では frontend が `NEXT_PUBLIC_BASIC_AUTH_*` をバンドルし、`next.config` rewrites で backend へプロキシしていた。
これだとパスワードがクライアント JS に漏れ、ログイン UI / ログアウト / CSRF 境界も弱い。

## 決定

### 二層構成

1. **backend (FastAPI)** — 引き続き **HTTP Basic 認証**(`ADMIN_USERNAME` / `ADMIN_PASSWORD`)。LAN 直叩き・運用スクリプト用。
   - `ADMIN_PASSWORD` 空は起動時に拒否
   - `/docs` / `/redoc` / `/openapi.json` は無効(契約 YAML を正とする)
2. **frontend (Next.js)** — **独自ログイン画面 `/login` + 署名付きセッション cookie**。
   - サーバ側で提出クレデンシャルを `ADMIN_*` と timing-safe 比較
   - `AUTH_SESSION_SECRET`(UTF-8 **32 バイト以上**)で HMAC-SHA256 署名した短命トークンを **HttpOnly / SameSite=Lax** cookie に格納(`Secure` は `AUTH_COOKIE_SECURE` または HTTPS 時)
   - middleware で管理画面と `/api/backend/*` を保護(未認証ページは `/login` へ、API は 401)
   - ログイン後 `next` は `safeNextPath` で同一オリジン相対パスのみ許可(オープンリダイレクト拒否)
   - `/api/backend/[...path]` **BFF Route Handler** がセッション検証後、サーバ側で Basic Authorization を注入して backend へ転送(SSE / 音声 / ダウンロード含むストリーム)。`.` / `..` セグメントは拒否。upstream は `Accept-Encoding: identity`
   - login / logout / BFF の unsafe メソッドは Origin/Referer の same-origin チェックで CSRF を緩和(LAN の Host 一致で利用可)
   - ログアウトは **POST のみ**(GET 廃止)。UI はボタンから POST
   - ログイン失敗はプロセス内インメモリの **グローバルバケット**でスロットリング(単一 Node / 単一 admin 前提。`X-Forwarded-For` は信頼しない)
   - compose の frontend は `.env` wholesale ではなく、認証関連の明示 env のみ注入
   - **`NEXT_PUBLIC_*` 資格情報は廃止**(クライアントに資格情報を埋め込まない)

### 運用前提

- ユーザー名/パスワード/`AUTH_SESSION_SECRET` は **環境変数**(`.env`)で管理、`.gitignore` で除外
- 1人運用前提のため **単一ユーザー**(複数ユーザー対応は将来課題)
- バックエンドの bind: **LAN 内 IP** にバインド(`0.0.0.0` ではなく明示指定推奨)
- 当面 HTTPS なし(LAN 内信頼前提) → `AUTH_COOKIE_SECURE=false`
- 将来の拡張余地:
  - 外出先からアクセスしたくなったら **Tailscale / WireGuard** を被せる
  - LAN 内 MitM 懸念が出たら self-signed cert + HTTPS or Caddy local-CA を導入

## 結果

### 良い影響

- LAN 内のタブレット・別PC から操作可能(柔軟な運用)
- パスワードが frontend バンドルに出ない
- ログイン UI / ログアウト / セッション期限が明確
- backend Basic は運用 curl / スクリプトと互換のまま

### 悪い影響・トレードオフ

- frontend と backend で認証レイヤが二重(BFF が橋渡しする分の複雑さ)
- グローバル in-memory rate limit はマルチレプリカでは共有されず、正当な失敗も全体でカウントされる(単一プロセス・単一 admin 前提で受容)
- HTTPS なしだと LAN 内 MitM の可能性
  - 緩和: LAN 内信頼前提で当面受容、必要時に HTTPS or VPN を追加

### 受容したリスク

- LAN 内に侵入された場合の防御線は薄い(WiFi WPA2/3 信頼前提)
- パスワードリセット運用が手動(1人運用なので許容)

## 検討した代替案

- **localhost のみ + 認証なし:** SSH ポートフォワード前提。タブレット等 LAN 内デバイスからのアクセスが不便。不採用。
- **ブラウザ標準 Basic のみ / `NEXT_PUBLIC_BASIC_AUTH_*`:** 実装は単純だが資格情報がクライアントに漏れる。不採用(本更新で廃止)。
- **クライアント IP ベースの login throttle(`X-Forwarded-For`):** ヘッダ捏造でバイパス可能。単一 admin ではグローバルバケットを採用。信頼プロキシ導入時に再検討。
- **Tailscale + Basic 認証:** より安全だが、当面の運用要件(LAN 内のみ)に対しては設定負担が大きい。将来必要になったら追加可能。不採用(現時点)。
- **NextAuth / Auth.js / Clerk + OAuth:** 1人運用・LAN 前提にはオーバーキル、外部依存が増える。不採用。

## 関連

- ADR-0001: バックエンド/フロントエンド
- ADR-0012: OAuth2 トークン保管
- ../requirements.md §機能要件(管理UI)
- ../screen-spec.md §4 アクセス制御 / 認証
