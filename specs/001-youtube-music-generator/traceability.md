# 仕様↔テスト トレーサビリティ・マトリクス

**対象**: `001-youtube-music-generator` / **正本**: [spec.md](./spec.md)(FR-001〜FR-123)
**方針**: [test-strategy.md](./test-strategy.md)

> この表は「どの FR をどのテストが満たすか」の一覧。 正本は各テストの `@pytest.mark.fr(...)`
> マーカー(backend)と `[FR-xxx]` テスト名(frontend)で、 この表はその人間可読な索引。
> **網羅の機械保証**は `backend/tests/test_requirement_coverage.py`(spec.md の全 FR にマーカー付き
> テストが存在することを静的検証)が担う。 FR を spec に足してテストを足さないと CI が赤になる。

凡例 — 種別: `unit`=単体 / `integration`=実DB / `critical`=critical path(100%) /
`static`=インフラ静的契約 / `e2e`=Playwright / `xfail`=仕様未充足を明示。
パスは backend は `backend/tests/` 起点、 frontend は `frontend/tests/` 起点。

## コア投稿フロー / コンプラ事前チェック

| FR | 要件 | 代表テスト | 種別 |
|----|------|-----------|------|
| FR-001 | 日次1〜2本生成→dryrun OFF で投稿 | unit/test_daily_cycle.py::test_post_path_uploads_when_not_dryrun | unit |
| FR-002 | 5分×6連結30分尺(acrossfade) | unit/test_video_compose.py::test_build_command_acrossfade_count_is_track_count_minus_one | unit |
| FR-003 | ACE-Step で音楽生成 | unit/test_music_jobs.py::test_submit_and_wait_creates_six_tracks_and_jobs | unit(worker mock) |
| FR-004 | SDXL(Juggernaut)でサムネ背景 | unit/test_image_jobs.py::test_submit_builds_prompt_from_visual_direction | unit(model 選択 assert) |
| FR-005 | showwaves overlay + 背景で合成 | unit/test_video_compose.py::test_build_command_filter_uses_acrossfade_and_showwaves | unit |
| FR-006 | containsSyntheticMedia=true 設定 | unit/test_youtube.py::test_upload_sets_synthetic_media_flag_and_updates_post | unit |
| FR-007 | 未設定なら投稿停止 | unit/test_youtube.py::test_compliance_gate_raises_when_flag_missing + critical/test_compliance_validation.py | unit/critical |
| FR-010 | 6トラック全指紋プレチェック | critical/test_acoustid_precheck.py::test_check_post_tracks_all_clear | critical |
| FR-011 | NG トラックのみ再生成 | unit/test_daily_cycle.py::test_hit_track_is_regenerated_then_clears | unit |
| FR-012 | 連続3ヒットでジャンル一時停止 | critical/test_acoustid_precheck.py::test_check_post_tracks_third_consecutive_hit_suspends_genre | critical |

> GPU 実機推論(ACE-Step/SDXL)と実 ffmpeg/fpcalc は worker/subprocess を mock した契約境界まで検証。
> 実バイナリの生成品質は GPU 実機での手動確認(quickstart.md / MVP checklist)。

## LLM プロバイダ / 改善計画

| FR | 要件 | 代表テスト | 種別 |
|----|------|-----------|------|
| FR-020 | Provider 3種切替 | critical/test_llm_provider_swap.py::test_create_llm_provider_defaults_to_env_openai | critical |
| FR-021 | OpenAI api_key/Codex 2モード | unit/test_config_llm_auth.py(起動時検証)+ unit/test_llm_api.py::test_set_provider_codex_oauth_non_openai_is_400 | unit |
| FR-022 | Anthropic サブスク起動時拒否 | critical/test_llm_provider.py::test_anthropic_rejects_subscription_auth_mode | critical |
| FR-023 | Pydantic v2 構造化検証 | critical/test_planner_schema.py::test_valid_plan_with_single_post_passes | critical |
| FR-024 | 検証失敗で最大2回 temp下げリトライ | critical/test_llm_provider.py::test_openai_retries_on_validation_failure_with_temperature_decay | critical |
| FR-025 | 全呼び出しを usage_log 記録 | unit/test_usage_writer.py(新規) | unit |
| FR-026 | 月予算 50/80/100% で Slack 通知 | unit/test_budget_alert.py::test_alerts_when_crossing_80_pct_first_time | unit |
| FR-027 | prompt caching 効く system prompt | unit/test_plans_planner.py::test_generate_uses_system_prompt_message | unit |
| FR-030 | DailyPlan を Pydantic 生成 | unit/test_plans_planner.py::test_generate_returns_validated_plan_and_usage | unit |
| FR-031 | WeeklyPlan 生成 | unit/test_weekly_planner.py::test_generate_returns_validated_plan_and_usage | unit/integration |
| FR-032 | genre を辞書照合 validate | critical/test_planner_schema.py::test_genre_not_in_allowed_list_is_rejected | critical |
| FR-033 | posts を 1〜2 件に制限 | critical/test_planner_schema.py::test_three_posts_is_rejected | critical |
| FR-034 | genre_distribution 合計 1.0±0.01 | unit/test_weekly_planner.py(成立 + 負経路を新規追加) | unit |
| FR-035 | 入力 metrics を snapshot 保存 | integration/test_weekly_cycle.py::test_weekly_cycle_generates_plan_and_applies_rotation | integration |
| FR-036 | prompt をバージョン管理・記録 | unit/test_plans_planner.py::test_generate_injects_allowed_genres_into_user_prompt | unit |
| FR-037 | experiment ジャンル採用/削除判定 | unit/test_genres_rotation_recommend.py::test_evaluate_adopt_at_or_above_080 + e2e/genres.spec.ts | unit/e2e |
| FR-038 | role 遷移を UI 承認+audit | unit/test_genres_api.py::test_promote_steps_up_one_level_and_enables + e2e/genres.spec.ts(promote/disable 承認導線) | unit/e2e |

## Directive parser / 仕上げ LLM / テンプレ・レンダ

| FR | 要件 | 代表テスト | 種別 |
|----|------|-----------|------|
| FR-040 | directive 形式自動判別 | critical/test_directive_parser.py::test_single_identifier_is_variable | critical |
| FR-041 | var=埋込 / 自由文=仕上げ生成 | critical/test_directive_parser.py::test_render_resolves_variable_from_context + unit/test_finisher.py | critical/unit |
| FR-042 | 仕上げに Haiku 級安価 model | unit/test_finisher.py::test_render_caps_max_tokens_for_cheap_budget(新規) | unit |
| FR-050 | ジャンル別テンプレ保有 | unit/test_templates_loader.py(新規: 読込/トラバーサル拒否/破損 fatal) | unit |
| FR-051 | タイトル形式 + 60字/サブ12字/絵文字1個 | unit/test_title_render.py::test_render_title_rejects_subtitle_over_12_chars / ::test_render_title_rejects_more_than_one_emoji | unit |
| FR-052 | 説明文4部構成 | unit/test_render_description.py::test_render_description_builds_six_chapter_lines | unit |
| FR-053 | AI開示は逐語固定文(LLM非生成) | unit/test_render_description.py::test_ai_disclosure_is_shared_verbatim_not_llm_generated(新規) | unit |
| FR-054 | SDXL背景+Pillow合成 | unit/test_thumbnail_overlay.py::test_compose_returns_resolved_uri_and_writes_jpeg | unit |
| FR-055 | フォント/配色を YAML で切替 | unit/test_thumbnail_overlay.py::test_unbundled_font_falls_back_without_error | unit |

> FR-051: タイトル形式・60 字総量・**サブタイトル ≤12 字**・**絵文字 ≤1 個**をすべて
> `title.py` でコード強制(超過は QualityError)。 絵文字数は ZWJ 結合を 1 個として数える。

## dryrun / スケジューラ

| FR | 要件 | 代表テスト | 種別 |
|----|------|-----------|------|
| FR-060 | dryrun で投稿以外を本番経路 | unit/test_scheduler.py::test_put_mode_disable_dryrun_audits_dryrun_disabled | unit |
| FR-061 | dryrun_outputs に5状態 | unit/test_dryrun_service.py::test_approve_uploads_and_sets_posted | unit/integration |
| FR-062 | pending を7日後 auto_expired+削除 | unit/test_dryrun_retention_job.py::test_expires_pending_older_than_retention_days | unit/integration |
| FR-063 | 否認理由を改善計画 LLM 入力に活用 | unit/test_plans_planner_rejected_reasons.py::test_explicit_rejected_reasons_injected_into_prompt | unit |
| FR-070 | APScheduler を backend 内実行 | unit/test_scheduler.py::test_add_daily_jobs_registers_both_slots | unit |
| FR-071 | scheduler_enabled フラグ制御 | unit/test_scheduler.py::test_get_scheduler_reads_persisted_true | unit |
| FR-072 | reboot 後は scheduler_enabled=false | unit/test_lifespan_scheduler.py(新規: `_read_scheduler_enabled` 正規化) | unit |
| FR-073 | 管理 UI から ON/OFF | unit/test_scheduler.py::test_put_scheduler_enable_audits_and_adds_jobs + e2e/scheduler.spec.ts | unit/e2e |

## 基盤 / GPU worker / YouTube

| FR | 要件 | 代表テスト | 種別 |
|----|------|-----------|------|
| FR-080 | Python+FastAPI(uv) | test_infra_contracts.py::test_backend_is_python_fastapi_uv_managed | static |
| FR-081 | Next.js(App Router) | test_infra_contracts.py::test_frontend_is_nextjs | static |
| FR-082 | PostgreSQL+pgvector | integration/test_orm_migration_consistency.py::test_orm_matches_migration | integration |
| FR-083 | OAuth token を Fernet 暗号化保存 | critical/test_security_fernet.py::test_encrypt_decrypt_roundtrip | critical |
| FR-084 | Fernet 鍵を .env / .gitignore 除外 | test_infra_contracts.py::test_env_holding_fernet_key_is_gitignored | static |
| FR-085 | 管理 UI を Basic 認証保護 | unit/test_auth_http.py::test_protected_route_with_valid_credentials_returns_200 + e2e auth.test.ts | unit/e2e |
| FR-086 | OpenAPI+openapi-typescript 型生成 | test_infra_contracts.py::test_openapi_typescript_codegen_is_wired | static |
| FR-087 | fsspec ストレージ抽象 | unit/test_music_jobs.py::test_regenerate_track_resets_acoustid_and_increments_count | unit |
| FR-090 | GPU worker を独立プロセス分離 | test_infra_contracts.py::test_gpu_worker_is_a_separate_package(+ gpu_worker/tests) | static |
| FR-091 | HTTP API + fsspec で疎結合 | unit/test_gpu_worker_swap_client.py::test_client_requests_only_its_own_base_url | unit |
| FR-092 | GPU_WORKER_BASE_URL を env 切替 | integration/test_gpu_worker_swap.py::test_base_url_flows_from_env_via_settings | integration |
| FR-093 | Dockerfile を CI で docker build | test_infra_contracts.py::test_gpu_worker_dockerfile_built_in_ci | static |
| FR-094 | RunPod 等へ移行可能な契約維持 | integration/test_gpu_worker_swap.py::test_swapping_settings_base_url_redirects_without_code_change | integration |
| FR-100 | upload+youtube+analytics スコープ | unit/test_oauth_scopes.py(新規。 **実装バグ修正済**) | unit |
| FR-101 | 週次 Analytics/Data API 取得 | critical/test_youtube_analytics_client.py::test_fetch_and_upsert_parses_all_metrics | critical |
| FR-102 | panic-stop で直近24h private 化 | critical/test_panic_stop.py::test_panic_stop_disables_lists_privatizes_and_audits | critical |

## 観測性 / エラー / バックアップ・デプロイ

| FR | 要件 | 代表テスト | 種別 |
|----|------|-----------|------|
| FR-110 | loguru 構造化 JSON ログ | unit/test_logging.py(新規) | unit |
| FR-111 | エラー5カテゴリ分類 | unit/test_error_categories.py(新規) | unit |
| FR-112 | compliance で停止+private化 | critical/test_compliance_validation.py::test_enforce_violation_raises_notifies_audits(+panic-stop) | critical |
| FR-113 | fatal で scheduler 停止+通知 | unit/test_scheduler.py::test_run_slot_disables_scheduler_on_halt_error / ::test_run_slot_keeps_scheduler_on_non_halt_fatal | unit |
| FR-114 | 単一 webhook + カテゴリ prefix | unit/test_slack_notifier.py::test_slack_prefix_uses_spec_category_names / ::test_notify_error_uses_compliance_prefix_for_compliance | unit |
| FR-115 | fatal/compliance に <!channel> | unit/test_slack_notifier.py(mention 検証、 新規) | unit |
| FR-120 | 日次 cron pg_dump + メタコピー | test_infra_contracts.py::test_daily_backup_timer_and_pg_dump | static |
| FR-121 | make migrate 前に自動 pg_dump | test_infra_contracts.py::test_migrate_takes_predump_before_alembic | static |
| FR-122 | Fernet 鍵をバックアップ除外 | test_infra_contracts.py::test_backup_excludes_fernet_key | static |
| FR-123 | デプロイは make 経由手動 | test_infra_contracts.py::test_deploy_is_a_manual_make_target | static |

## フロントエンド(画面 critical flow)

backend マーカーに加え、 frontend テスト名に `[FR-xxx]` を付与:
- `[FR-085]` unit/auth.test.ts(Basic 認証ヘッダ注入)
- `[FR-073/FR-102]` e2e/scheduler.spec.ts(scheduler ON/OFF・panic-stop UI)
- `[FR-020/FR-022]` e2e/llm.spec.ts(provider 切替・不正組合せ)
- `[FR-061/FR-063]` e2e/dryrun_review.spec.ts(承認/却下・却下理由)
- `[FR-037/FR-038]` e2e/genres.spec.ts(ジャンル管理: 昇格=採用 / 無効化=削除 の承認導線、 画面10)

## 仕様↔実装の乖離(調査で発見 → ユーザー判断のうえ解決済み)

調査で spec.md と実装の不一致を 4 件検出した。 ユーザー相談(2026-06-22)で「実装を仕様に合わせる」
判断を受け、 すべて**実装を修正して解決済み**。

1. **FR-100(解決済)**: OAuth フローが `youtube.upload` スコープしか要求せず、 FR-101(Analytics)/
   FR-102(privacy 変更)が本番で権限不足になる実装バグ。 → `oauth.py` に `youtube` /
   `yt-analytics.readonly` を追加し 3 スコープ要求に修正(`YOUTUBE_SCOPES`)。 `test_oauth_scopes.py` で固定。
2. **FR-113(解決済)**: fatal で当該サイクルを中断するのみで scheduler を止めていなかった。 →
   **特定 fatal のみ停止**方針を採用。 インフラ級 fatal を表す `SchedulerHaltError(FatalError)` を新設し、
   DB 不達等はこれを送出 → scheduler job wrapper が捕捉して `disable()`。 個別 post の content fatal は
   従来どおりサイクル中断のみ(scheduler 継続)。 `test_scheduler.py` の halt/非 halt 2 テストで検証。
3. **FR-114(解決済)**: prefix が level ベース(`[ERROR]/[WARN]/[INFO]`)で spec.md のカテゴリ名と不一致
   だった。 → `slack_prefix_for` を**カテゴリ名 prefix**(`[COMPLIANCE]/[TRANSIENT]/[RECOVERABLE]/
   [QUALITY]/[FATAL]`)に変更(`_CATEGORY_SLACK_PREFIX`)。 関連テストを更新。
4. **FR-051(解決済)**: 「サブタイトル ≤12 字」「絵文字 1 個まで」が未強制だった。 → `title.py` に
   生成スロット ≤12 字検証 + 最終タイトルの絵文字数 ≤1 検証(ZWJ 結合は 1 個)を追加。 超過は QualityError。

## 網羅状況サマリ

- spec.md の機能要件 **FR 70 件すべて**にマーカー付きテストが存在(`test_requirement_coverage.py` が CI で保証)。
- 仕様↔実装の乖離 4 件はすべて実装修正で解決済み(残る xfail は FR-072 の取得例外経路 1 件のみ — 安全側 false 化は lifespan の DB 接続検証が先行担保するため helper 単体では再現不能、という設計上の注記)。
- 自動テスト対象外として除外した FR は **0 件**(インフラ系も静的契約テストで担保)。
