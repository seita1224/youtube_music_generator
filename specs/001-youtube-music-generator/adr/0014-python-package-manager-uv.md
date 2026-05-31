# ADR-0014: Python パッケージ管理 = uv

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, infra, devex

## 背景

Python(ADR-0001)プロジェクトのパッケージ管理ツール選定。
PyTorch / ACE-Step / diffusers など重いライブラリを多数扱うため、インストール速度と再現性が重要。

## 決定

- **uv**(Astral 製)を採用
- 依存定義: `pyproject.toml`
- ロックファイル: `uv.lock`(コミット対象)
- Python 自体のインストールも uv に任せる(`uv python install`)
- 仮想環境: `.venv`(uv が自動管理)
- CI / 本番起動でも uv を使用

## 結果

### 良い影響

- インストール・解決が高速(Rust 実装、pip / poetry より大幅に速い)
- Python バージョン管理まで内包(pyenv 等の併用不要)
- `pyproject.toml` 一本で完結、PEP 準拠
- ロックファイルにより環境再現性が確保される

### 悪い影響・トレードオフ

- 比較的新しいツールのため、情報量は poetry より少ない(ただし急速に増加中)
- 一部のエッジケース(プライベートインデックスの認証等)では情報が薄い場合がある
  - 緩和: 公式 docs と GitHub issue で対応可能

### 受容したリスク

- 将来 Astral の方針変更がありうる → そのときに poetry 等への移行は `pyproject.toml` 中心なので可能

## 検討した代替案

- **poetry:** 老舗で安定、情報量豊富。速度で uv に劣る、Python 自体の管理は別ツール必須。不採用。
- **pip + venv + pip-tools:** 標準だが手作業が多い、ロックの扱いが煩雑。不採用。
- **rye:** uv に統合された経緯あり、uv そのものを使う方が直球。不採用。

## 関連

- ADR-0001: バックエンド = Python
- ../requirements.md §システム構成
- uv: <https://github.com/astral-sh/uv>
