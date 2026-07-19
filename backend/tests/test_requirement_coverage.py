"""機能要件(FR)網羅ゲート — spec.md ↔ テストのトレーサビリティを機械検証する。

このメタテストが「全仕様を漏れなくテストする」ことを仕組みで保証する(人手の注意に頼らない)。

検証内容:
  1. spec.md を**単一の正本**として全 FR ID を抽出する。
  2. backend テストの ``@pytest.mark.fr("FR-xxx")`` 引数と、 frontend テストの
     ``[FR-xxx]`` 表記を静的走査して「テストが宣言する FR 集合」を得る。
  3. ``MANUAL_FRS``(自動テスト不能 + 理由)を引いてもなお未カバーの FR があれば fail。
  4. 存在しない FR を指すマーカー / 古い ``MANUAL_FRS`` エントリ(腐敗)も fail。

runtime のテスト収集に依存せず**ソースを静的走査**するため、 ``pytest`` を部分実行しても結果は
不変。 詳細方針は specs/001-youtube-music-generator/test-strategy.md を参照。
"""

from __future__ import annotations

import re
from pathlib import Path

# backend/tests/ から repo ルートを辿る(tests/ -> backend/ -> repo)。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = _REPO_ROOT / "specs" / "001-youtube-music-generator" / "spec.md"
_BACKEND_TESTS = _REPO_ROOT / "backend" / "tests"
_FRONTEND_TESTS = _REPO_ROOT / "frontend" / "tests"

_FR_TOKEN = re.compile(r"\bFR-\d{3}\b")
# ``@pytest.mark.fr("FR-006", "FR-007")`` の引数部だけを取り出す(複数行は想定しない)。
_FR_MARKER_CALL = re.compile(r"\.fr\(([^)]*)\)")


def _spec_requirements() -> set[str]:
    """spec.md の Functional Requirements 節から全 FR ID を抽出する(正本)。"""
    text = _SPEC.read_text(encoding="utf-8")
    # "### Functional Requirements" 以降〜 "### Key Entities" 手前までを対象に絞る。
    start = text.find("### Functional Requirements")
    end = text.find("### Key Entities")
    section = text[start:end] if start != -1 and end != -1 else text
    return set(_FR_TOKEN.findall(section))


def _markered_requirements() -> set[str]:
    """backend テストの ``pytest.mark.fr(...)`` 引数から FR ID を収集する。"""
    found: set[str] = set()
    for path in _BACKEND_TESTS.rglob("test_*.py"):
        if path.name == Path(__file__).name:
            continue  # 自分自身(メタテスト)は除外。
        src = path.read_text(encoding="utf-8")
        for args in _FR_MARKER_CALL.findall(src):
            found.update(_FR_TOKEN.findall(args))
    return found


def _frontend_requirements() -> set[str]:
    """frontend テストのタイトル/コメント中の ``FR-xxx`` 表記を収集する。"""
    found: set[str] = set()
    if not _FRONTEND_TESTS.exists():
        return found
    for pattern in ("*.test.ts", "*.test.tsx", "*.spec.ts", "*.spec.tsx"):
        for path in _FRONTEND_TESTS.rglob(pattern):
            found.update(_FR_TOKEN.findall(path.read_text(encoding="utf-8")))
    return found


# --- 自動テスト対象外の FR(理由必須)-------------------------------------------------
# ここに無い FR は「自動テストで担保する」契約。 各エントリは spec.md の実在 FR でなければならない
# (腐敗検出のため)。 区分: infra-ci(技術選定/ビルド成立で担保) / manual(GPU 実機・実投稿・
# cron/デプロイ等、 実環境でのみ検証可能)。 個別の代替検証手段は traceability.md を参照。
#
# 現状は **空**。 インフラ/デプロイ系(FR-080/081/084/086/090/093/120-123)も
# tests/test_infra_contracts.py の静的契約テストで担保し、 GPU/実投稿系も契約境界(worker
# mock / 関数境界)で auto 検証しているため、 全 FR が fr マーカー付きテストで説明される。
# 自動化不能な FR が将来生じたら、 ここに "FR-xxx": "理由(区分含む)" を追加する。
MANUAL_FRS: dict[str, str] = {}


def test_all_functional_requirements_are_covered() -> None:
    """全 FR が「fr マーカー付きテスト」か「理由付き MANUAL_FRS」で説明されていること。"""
    spec = _spec_requirements()
    assert spec, "spec.md から FR を抽出できなかった(パス/見出しを確認)"

    covered = _markered_requirements() | _frontend_requirements()
    manual = set(MANUAL_FRS)

    missing = sorted(spec - covered - manual)
    assert not missing, (
        f"未テストの FR が {len(missing)} 件: {missing}\n"
        "→ 対応する test に @pytest.mark.fr('FR-xxx') を付けるか、 "
        "自動化不能なら MANUAL_FRS に理由付きで登録すること。"
    )


def test_no_stale_or_typo_requirement_tags() -> None:
    """マーカー / MANUAL_FRS が spec.md に実在しない FR を指していないこと(腐敗検出)。"""
    spec = _spec_requirements()
    tagged = _markered_requirements() | _frontend_requirements() | set(MANUAL_FRS)
    unknown = sorted(tagged - spec)
    assert not unknown, (
        f"spec.md に存在しない FR を参照している: {unknown}\n"
        "→ タイポか、 spec.md から削除された FR。 修正すること。"
    )


def test_meta_self_check_spec_is_parseable() -> None:
    """メタテスト自身の健全性 — spec.md が読め、 既知の FR が含まれること。"""
    spec = _spec_requirements()
    assert {"FR-001", "FR-072", "FR-123"} <= spec
    assert len(spec) >= 70  # spec.md の FR は 70 件(退行検出)
