# ADR-0030: git 運用ポリシー(リポジトリ公開設定・ブランチ・コミット規約)

- **ステータス:** Accepted
- **日付:** 2026-05-26
- **決定者:** @seita
- **タグ:** ops / policy

## 背景

ADR-0029 でモノレポ構成が確定した。git 運用について以下の3点を決める必要があった。

1. GitHub リポジトリの公開設定(Public / Private)
2. ブランチ戦略(trunk-based / feature + PR / GitHub Flow)
3. コミットメッセージ規約(global ルールに対するプロジェクト固有の追加)

前提:

- 1人運用、本人のみが触る
- 動機 C(マネタイズ)が確定しており、運用ノウハウ・プロンプト・ジャンル選定ロジックは競争優位の源泉
- ADR-0019 で Codex OAuth 経路(マネタイズ用途 ToS グレー)を残しており、公開リスクがある
- 本番は ローカル GPU マシン上の systemd プロセスで、デプロイは `git pull && systemctl restart`
- ADR-0012 で OAuth トークン Fernet 鍵は `.env` で管理し `.gitignore` で除外済み

## 決定

### (1) リポジトリは **Private**

GitHub Private repository として運用する。Public 化は「運用が安定し、晒しても問題ないと判断できた時」に再評価する。

### (2) ブランチ戦略 = **main + feature ブランチ + PR self-merge**

- `main` は常にデプロイ可能な状態を維持
- 作業は `feature/<short-name>` または `fix/<short-name>` ブランチで行う
- PR を作成し、CI(lint / 型 / テスト)が green になってから self-merge
- release tag は **打たない**(YAGNI、必要になった時点で再評価)
- Squash merge を基本とする(`main` の履歴を 1 PR = 1 commit に保つ)

### (3) コミットメッセージ規約 = **Global ルール + scope 推奨**

global ルール(`~/.claude/rules/common/git-workflow.md`)の `<type>: <description>` 形式を踏襲しつつ、モノレポ事情に合わせて scope を **推奨(必須ではない)** とする。

形式:

```text
<type>(<scope>): <description>

<optional body>
```

- types: `feat / fix / refactor / docs / test / chore / perf / ci`(global と同一)
- scope の候補: `backend / frontend / infra / adr / docs / ci`
- scope は迷ったら付ける(`feat(backend): ...` / `docs(adr): ...`)、自明な場合は省略可
- Conventional Commits の breaking change マーカー(`!`)や footer は使わない

例:

```text
feat(backend): add APScheduler daily job
fix(backend): containsSyntheticMedia の必須化バリデーション
docs(adr): ADR-0030 git 運用ポリシーを追加
feat(frontend): ジャンル選択画面の UI 実装
```

## 結果

### 良い影響

- **Private:** プロンプト・ジャンル選定ロジック・Codex OAuth 経路(グレーゾーン)が外部に晒されない。誤コミット時の被害が内部に閉じる
- **PR self-merge:** CI gate(lint / 型 / テスト)が main 到達前に効くため、壊れたコードが production main に入る瞬間がなくなる
- **scope 推奨:** モノレポで backend/frontend/infra が同居するため、`git log --oneline` で変更箇所が一目でわかる
- 1人運用ながら将来の自分(数ヶ月後)向けの可読性を担保

### 悪い影響・トレードオフ

- Private のためポートフォリオ的価値は失われる(動機が C 中心なので許容)
- PR 経由は trunk-based より workflow が 1 ステップ多い(CI 完走待ち含めて数分のオーバーヘッド)
- release tag を打たないため、`git checkout vX.Y.Z` でのロールバックはできず、ロールバックは「直前 commit に reset」もしくは「revert commit」での対応
- 反転コスト:
  - Public 化は履歴ごと公開になるため、誤コミットされた secrets が過去にないことを `git-secrets` 等で検査してから切り替え
  - release tag 導入は後付け可能

### 受容したリスク

- `.env` 等の secrets 誤コミットは Private でも GitHub 内部に履歴が残るため、検出時は履歴ごと filter-branch + 鍵ローテーションが必要
- 1人運用の PR self-merge は「セルフレビュー」の質に依存する。 客観性が落ちる場面では `code-reviewer` agent / `/ultrareview` を併用する

## 検討した代替案

### リポジトリ公開設定

- **代替案: Public:** ポートフォリオ価値・対外発信メリットあり。マネタイズ方針 + Codex OAuth のグレー運用を晒すリスクの方が大きいと判断、不採用
- **代替案: 最初 Private → 後で Public:** 履歴ごと公開になるため secrets 履歴の検査が事前必須。判断は将来時点で再評価可能なため、 一旦 Private で固定

### ブランチ戦略

- **代替案A: main 直 push (trunk-based):** CI gate が main 直に走るため壊れたコードが production main に入る瞬間が必ず存在。不採用
- **代替案C: main + feature + dev 三層:** 1人運用には過剰、不採用
- **代替案D: GitHub Flow(main + feature + PR + release tag):** tag によるロールバックは魅力的だが、現状の運用規模では YAGNI。必要になれば後付け可能

### コミット規約

- **代替案A: scope なし(global のみ):** モノレポで変更箇所が title から消えるため不採用
- **代替案B: Conventional Commits 完全準拠:** breaking change マーカー・footer の運用負担過多、不採用

## 関連

- ADR-0006: サイクル構造(日次+週次)
- ADR-0012: OAuth トークン暗号化方針
- ADR-0019: LLMプロバイダ抽象化(Codex OAuth のグレーゾーン)
- ADR-0029: モノレポ構成
- `~/.claude/rules/common/git-workflow.md`: global commit ルール
