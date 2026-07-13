"""SQLAlchemy ORM モデル (T021)。

1 ファイル 1 entity でテーブルを定義する。 すべて
`alembic/versions/` の手書き DDL / `data-model.md` と完全整合させる。

`Base.metadata` は将来 `alembic/env.py` の autogenerate 対象に割り当てられる前提
(env.py のコメント参照)。 そのため全モデルをここで import / re-export し、
`Base.metadata` に確実に登録されるようにする。
"""

from __future__ import annotations

from .analytics_daily import AnalyticsDaily
from .app_state import AppState
from .audio_track import AudioTrack
from .audit_log import AuditLog
from .base import Base
from .comment import Comment
from .dryrun_output import DryrunOutput
from .genre import Genre
from .gpu_job import GpuJob
from .job_history import MUSIC_GENERATION_JOB_NAME, JobHistory
from .job_step_event import JobStepEvent
from .llm_provider_secret import LlmProviderSecret
from .model_pricing import ModelPricing
from .oauth_credential import OAuthCredential
from .plan import Plan
from .plan_metric_snapshot import PlanMetricSnapshot
from .post import Post
from .usage_log import UsageLog
from .video import Video

__all__ = [
    "MUSIC_GENERATION_JOB_NAME",
    "AnalyticsDaily",
    "AppState",
    "AudioTrack",
    "AuditLog",
    "Base",
    "Comment",
    "DryrunOutput",
    "Genre",
    "GpuJob",
    "JobHistory",
    "JobStepEvent",
    "LlmProviderSecret",
    "ModelPricing",
    "OAuthCredential",
    "Plan",
    "PlanMetricSnapshot",
    "Post",
    "UsageLog",
    "Video",
]
