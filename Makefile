# YouTube 音楽投稿自動化システム — 運用 Makefile (ADR-0031)
# docker compose (backend/frontend/postgres) + host 直 GPU worker (systemd) のハイブリッド構成。
# 緊急停止は `make panic-stop` ではなくシステム状態 (`ymg stop` / `ymg pause-publishing`) で行う (ADR-0044)。

SHELL := /bin/bash
COMPOSE := docker compose
TS := $(shell date +%Y%m%d-%H%M%S)
BACKUP_DIR ?= $(shell . ./.env 2>/dev/null; echo $${BACKUP_ROOT:-/srv/ymg/backups})

.DEFAULT_GOAL := help

.PHONY: help up down migrate migrate-legacy restart-backend restart-gpu logs \
        deploy backup restore-db youtube-auth test test-critical healthcheck \
        lint fmt typecheck

help: ## このヘルプを表示
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## backend/frontend/postgres を起動 (GPU worker は systemd 側)
	$(COMPOSE) up -d postgres backend frontend

down: ## スタックを停止
	$(COMPOSE) down

logs: ## 全サービスのログを追従
	$(COMPOSE) logs -f --tail=100

restart-backend: ## backend のみ再起動
	$(COMPOSE) restart backend

restart-gpu: ## GPU worker (systemd) を再起動
	sudo systemctl restart ymg-gpu-worker

migrate: ## DB マイグレーション (事前 pg_dump 込, ADR-0031)
	@mkdir -p $(BACKUP_DIR)
	@echo "[migrate] pre-dump → $(BACKUP_DIR)/pre-migrate-$(TS).sql"
	$(COMPOSE) exec -T postgres sh -c 'pg_dump -U "$$POSTGRES_USER" "$$POSTGRES_DB"' > $(BACKUP_DIR)/pre-migrate-$(TS).sql
	$(COMPOSE) exec -T backend uv run alembic upgrade head

deploy: ## git pull → migrate → rebuild → up → healthcheck (ADR-0031)
	bash infra/scripts/deploy.sh

backup: ## pg_dump + メタデータを BACKUP_ROOT へ (Fernet 鍵は除外, ADR-0026)
	bash infra/scripts/backup.sh

restore-db: ## DB を DUMP=path から復元 (例: make restore-db DUMP=backups/db-xxx.sql)
	@test -n "$(DUMP)" || { echo "DUMP=<path> を指定してください"; exit 1; }
	$(COMPOSE) exec -T postgres sh -c 'psql -U "$$POSTGRES_USER" "$$POSTGRES_DB"' < $(DUMP)

youtube-auth: ## YouTube OAuth を一度だけ手動完走
	@echo "[youtube-auth] TODO: backend OAuth flow"

migrate-legacy: ## 旧 DB から公開実績のみを 1 回きり移行 (ADR-0050, T077 で実装)
	@echo "[migrate-legacy] TODO: uv run ymg migrate-legacy (T077)"

healthcheck: ## backend/frontend/gpu_worker の /health を確認
	bash infra/scripts/healthcheck.sh

test: ## backend 全テスト
	cd backend && uv run pytest -q

# critical path 8 系統 (憲法 II v2.0.0 / plan.md)。 状態機械 / INV-2 / 無効化 /
# compliance 自動停止のモジュールは実装タスク (T016/T019/T021/T047/T054) で追加する。
test-critical: ## critical path モジュールを 100% カバレッジ強制 (Constitution II)
	cd backend && uv run pytest tests/critical \
	  --cov=ymg_backend.core.security \
	  --cov=ymg_backend.domain.directive.parser \
	  --cov=ymg_backend.domain.compliance.validators \
	  --cov=ymg_backend.domain.compliance.acoustid \
	  --cov=ymg_backend.llm.factory \
	  --cov-report=term-missing --cov-fail-under=100

lint: ## ruff + eslint
	cd backend && uv run ruff check .
	cd frontend && npm run lint

fmt: ## ruff format
	cd backend && uv run ruff format .

typecheck: ## mypy + tsc
	cd backend && uv run mypy src
	cd frontend && npx tsc --noEmit
