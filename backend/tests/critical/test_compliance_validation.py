"""containsSyntheticMedia 投稿前バリデーションのクリティカルパステスト (T069)。

Constitution II / Compliance-First (ADR-0020, ADR-0028) を 100% カバーする。

検証観点:

- `status.containsSyntheticMedia=true` が無い / false / 非 bool の upload body を
  投稿しようとすると ``ComplianceError`` が raise され、 投稿が停止する。
- 違反時に Slack 通知 (ERROR レベル / ``[ERROR]`` prefix) が必ず送られる。
- 違反時に ``audit_log`` へ ``compliance_violation`` が 1 行記録される。
- 正常 body (``containsSyntheticMedia=true``) は素通りし、 Slack / audit は呼ばれない。
- DB-only / Slack-only の単体検証も独立して通る (ガード層と通知層の責務分離)。

Slack はスパイ (記録用 fake notifier)、 audit_log は ``AsyncSession`` のスパイで検証する。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from ymg_backend.domain.compliance.validators import (
    COMPLIANCE_AUDIT_ACTION,
    SyntheticMediaViolationNotice,
    assert_contains_synthetic_media,
    enforce_synthetic_media_compliance,
)
from ymg_backend.domain.errors import ComplianceError, ErrorCategory, NotificationLevel

pytestmark = pytest.mark.critical


# --- スパイ / fake ----------------------------------------------------------------


@dataclass
class SpyNotifier:
    """Slack 通知のスパイ。 送られた notice を記録するだけ。"""

    notices: list[SyntheticMediaViolationNotice] = field(default_factory=list)
    raise_on_notify: Exception | None = None

    async def notify_compliance_violation(self, notice: SyntheticMediaViolationNotice) -> None:
        if self.raise_on_notify is not None:
            raise self.raise_on_notify
        self.notices.append(notice)


@dataclass
class _ExecutedInsert:
    """記録した execute() 呼び出し (audit_log insert)。"""

    table_name: str
    values: dict[str, Any]


class SpySession:
    """``AsyncSession`` の最小スパイ。 ``write_audit_log`` が使う execute/flush のみ実装。"""

    def __init__(self) -> None:
        self.executed: list[_ExecutedInsert] = []
        self.flush_count = 0

    async def execute(self, statement: Any) -> None:
        compiled = statement.compile()
        table_name = statement.table.name
        self.executed.append(_ExecutedInsert(table_name=table_name, values=dict(compiled.params)))

    async def flush(self) -> None:
        self.flush_count += 1

    # --- helpers --------------------------------------------------------------
    @property
    def audit_inserts(self) -> list[_ExecutedInsert]:
        return [e for e in self.executed if e.table_name == "audit_log"]


def _valid_body() -> dict[str, Any]:
    return {
        "snippet": {
            "title": "Lo-Fi Beats",
            "description": "AI generated music.",
            "categoryId": "10",
            "tags": ["lofi"],
        },
        "status": {
            "privacyStatus": "public",
            "containsSyntheticMedia": True,
            "selfDeclaredMadeForKids": False,
        },
    }


# --- assert_contains_synthetic_media (純粋ガード) --------------------------------


def test_valid_body_passes() -> None:
    """containsSyntheticMedia=true の body は例外を出さない。"""
    assert_contains_synthetic_media(_valid_body(), video_ref="post-1")


def test_missing_status_block_raises() -> None:
    body: dict[str, Any] = {"snippet": {"title": "x"}}
    with pytest.raises(ComplianceError) as exc:
        assert_contains_synthetic_media(body, video_ref="post-1")
    assert exc.value.category is ErrorCategory.COMPLIANCE
    assert exc.value.context["video_ref"] == "post-1"
    assert exc.value.context["reason"] == "missing_status"


def test_missing_flag_raises() -> None:
    body: dict[str, Any] = {"status": {"privacyStatus": "public"}}
    with pytest.raises(ComplianceError) as exc:
        assert_contains_synthetic_media(body, video_ref="post-2")
    assert exc.value.context["reason"] == "missing_flag"


def test_flag_false_raises() -> None:
    body = _valid_body()
    body["status"]["containsSyntheticMedia"] = False
    with pytest.raises(ComplianceError) as exc:
        assert_contains_synthetic_media(body, video_ref="post-3")
    assert exc.value.context["reason"] == "flag_not_true"


def test_flag_non_bool_truthy_raises() -> None:
    """文字列 'true' 等の非 bool は厳格に拒否 (型の緩みを防ぐ)。"""
    body = _valid_body()
    body["status"]["containsSyntheticMedia"] = "true"
    with pytest.raises(ComplianceError) as exc:
        assert_contains_synthetic_media(body, video_ref="post-4")
    assert exc.value.context["reason"] == "flag_not_true"


def test_status_not_mapping_raises() -> None:
    body: dict[str, Any] = {"status": ["not", "a", "mapping"]}
    with pytest.raises(ComplianceError) as exc:
        assert_contains_synthetic_media(body, video_ref="post-5")
    assert exc.value.context["reason"] == "missing_status"


def test_body_not_mapping_raises() -> None:
    with pytest.raises(ComplianceError) as exc:
        assert_contains_synthetic_media(["not", "a", "dict"], video_ref="post-6")
    assert exc.value.context["reason"] == "body_not_mapping"


def test_blank_video_ref_rejected() -> None:
    with pytest.raises(ValueError, match="video_ref"):
        assert_contains_synthetic_media(_valid_body(), video_ref="  ")


# --- enforce_synthetic_media_compliance (ガード + Slack + audit) -----------------


async def test_enforce_valid_body_no_side_effects() -> None:
    notifier = SpyNotifier()
    session = SpySession()
    await enforce_synthetic_media_compliance(
        _valid_body(),
        video_ref="post-ok",
        session=session,
        notifier=notifier,
    )
    assert notifier.notices == []
    assert session.audit_inserts == []
    assert session.flush_count == 0


async def test_enforce_violation_raises_notifies_audits() -> None:
    notifier = SpyNotifier()
    session = SpySession()
    body = _valid_body()
    del body["status"]["containsSyntheticMedia"]

    with pytest.raises(ComplianceError) as exc:
        await enforce_synthetic_media_compliance(
            body,
            video_ref="post-bad",
            session=session,
            notifier=notifier,
            target_type="post",
            target_id="post-bad",
        )

    # 1) 投稿停止: compliance 例外
    assert exc.value.category is ErrorCategory.COMPLIANCE
    assert exc.value.context["reason"] == "missing_flag"

    # 2) Slack 通知 (1 回 / ERROR / prefix [ERROR])
    assert len(notifier.notices) == 1
    notice = notifier.notices[0]
    assert notice.level is NotificationLevel.ERROR
    assert notice.prefix == "[ERROR]"
    assert notice.video_ref == "post-bad"
    assert notice.reason == "missing_flag"
    assert "[ERROR]" in notice.message

    # 3) audit_log に compliance_violation を 1 行
    assert len(session.audit_inserts) == 1
    audit_values = session.audit_inserts[0].values
    assert audit_values["action"] == COMPLIANCE_AUDIT_ACTION
    assert audit_values["target_type"] == "post"
    assert audit_values["target_id"] == "post-bad"
    assert audit_values["payload"]["reason"] == "missing_flag"
    assert isinstance(audit_values["id"], uuid.UUID)
    assert session.flush_count == 1


async def test_enforce_audit_written_before_notify() -> None:
    """audit を先に永続化してから通知する (通知失敗でも証跡が残る)。"""
    session = SpySession()
    notifier = SpyNotifier(raise_on_notify=RuntimeError("slack down"))
    body = _valid_body()
    body["status"]["containsSyntheticMedia"] = False

    with pytest.raises(RuntimeError, match="slack down"):
        await enforce_synthetic_media_compliance(
            body,
            video_ref="post-x",
            session=session,
            notifier=notifier,
        )
    # 通知が落ちても audit は残っている
    assert len(session.audit_inserts) == 1
    assert session.audit_inserts[0].values["payload"]["reason"] == "flag_not_true"


async def test_enforce_default_target_id_falls_back_to_video_ref() -> None:
    notifier = SpyNotifier()
    session = SpySession()
    body = _valid_body()
    del body["status"]

    with pytest.raises(ComplianceError):
        await enforce_synthetic_media_compliance(
            body,
            video_ref="vref-7",
            session=session,
            notifier=notifier,
        )
    audit_values = session.audit_inserts[0].values
    assert audit_values["target_type"] == "video"
    assert audit_values["target_id"] == "vref-7"


# --- SyntheticMediaViolationNotice ------------------------------------------------


def test_notice_message_contains_context() -> None:
    notice = SyntheticMediaViolationNotice.from_violation(
        video_ref="vref-9", reason="missing_status"
    )
    assert notice.level is NotificationLevel.ERROR
    assert notice.prefix == "[ERROR]"
    assert "vref-9" in notice.message
    assert "missing_status" in notice.message
    assert notice.message.startswith("[ERROR]")
