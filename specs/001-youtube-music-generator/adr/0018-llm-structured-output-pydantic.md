# ADR-0018: LLM 構造化出力 = Pydantic + LLMProvider 抽象化層

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, ml

## 背景

LLM 出力を構造化する場面が複数:

- ディレクティブ展開(ADR-0017、1テンプレ内の複数フィールドを JSON で返却)
- 改善計画立案(週次サイクル、ADR-0006)
- ジャンル選定(日次サイクル、6 サブテーマ含む)
- コメント分析(週次サイクル)

各プロバイダで構造化出力 API が異なる:

- Anthropic: `tools` で `input_schema` を渡す方式 / JSON モード
- OpenAI: `response_format={"type": "json_schema", ...}`
- Ollama: `format="json"`(スキーマ強制はモデル依存)

LLMProvider 抽象化層(ADR-0002)で吸収しないと、呼び出し側コードが provider 依存になる。

## 決定

### スキーマ定義

- **Pydantic v2 の `BaseModel`** でスキーマを定義
- スキーマは用途別に明示的に名前を付ける(`GenreSelectionResult`、`ImprovementPlan`、`DirectiveExpansionResult` 等)
- 既存の FastAPI(ADR-0009)・SQLAlchemy ORM とも Pydantic で型統合

### LLMProvider 抽象化層の API

```python
class LLMProvider(Protocol):
    async def generate_structured[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        context: dict | None = None,
        max_retries: int = 3,
    ) -> T: ...
```

### 各 provider 実装

- **Anthropic**: 公式 SDK(`anthropic` Python パッケージ)を使用、`tools` パラメータ + `input_schema=schema.model_json_schema()`
- **OpenAI / OpenAI 互換 API**: 公式 SDK(`openai` Python パッケージ)、`response_format={"type": "json_schema", "json_schema": {"name": ..., "schema": ...}}`
- **Ollama**: 公式 SDK(`ollama` Python パッケージ)、`format="json"` + プロンプトでスキーマ提示 + Pydantic でバリデーション

### リトライ・フォールバック

- スキーマ違反(Pydantic ValidationError)時は **最大 N 回リトライ**(エラー文を LLM に返して再生成依頼)
- N 回失敗 → Slack 通知 + 該当ジョブスキップ(ADR-0006 と整合)
- 失敗内容(プロンプト・出力・エラー)は DB に記録(分析用)

### スキーマ進化

- フィールド追加: Optional + デフォルト値で後方互換
- 必須化・削除は別 ADR でマイグレーション計画を立てる

## 結果

### 良い影響

- スキーマ定義が一元化、FastAPI・SQLAlchemy ・LLM 呼出で同じ Pydantic 型を使い回せる
- LLMProvider 切替がコード変更なく可能
- リトライ・バリデーション・通知が一箇所に集約

### 悪い影響・トレードオフ

- 抽象化層の薄いラッパーを自前で保守する必要(各 SDK の破壊的変更追従)
  - 影響軽微: 各 SDK は安定、ラッパーは 100〜200 行程度
- スキーマ違反リトライは追加トークン消費を生む
  - 緩和: max_retries で上限管理、頻発時はプロンプト改善

### 受容したリスク

- Ollama のスキーマ強制はモデル依存で、強制力が弱い(特に小型モデル)
  - 対策: バリデーション + リトライで補う

## 検討した代替案

- **instructor ライブラリ:** 複数プロバイダ対応 + リトライ内蔵で便利だが、抽象化層を自前で書くコストは低く、依存追加の価値が薄い。不採用。
- **自由テキスト + 手書きパーサ:** 構造化失敗が頻発、保守コスト高。不採用。
- **LangChain / LlamaIndex の output parser:** フレームワーク全面採用が前提で本プロジェクトには重い。不採用。

## 関連

- ADR-0002: LLMProvider 抽象化層
- ADR-0008: LLM の責務範囲
- ADR-0009: FastAPI(Pydantic 統合)
- ADR-0017: ディレクティブパーサ
- ../requirements.md §使用モデル方針
