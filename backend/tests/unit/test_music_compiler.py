"""music_compiler (ADR-0039 / U3) の単体テスト。"""

from __future__ import annotations

import uuid

import pytest

from ymg_backend.domain.pipeline.music_compiler import (
    COMPILER_VERSION,
    TRACK_COUNT,
    TRACK_DURATION_SEC,
    MusicCompileError,
    compile_post,
    execution_spec_dict,
)
from ymg_backend.domain.plans.schemas import DailyPost, TrackLlmProposal
from ymg_backend.infrastructure.gpu_worker_client import music_request_from_execution_spec

_GENRE = "lo-fi hip-hop"
_VISUAL = "UNIQUE_VISUAL_MARKER_xyz_lamplight_notebook_coffee_scene"


def _proposals(
    *,
    desired_bpm: int = 80,
    desired_music_key: str = "C major",
    bpm_by_position: dict[int, int] | None = None,
    key_by_position: dict[int, str] | None = None,
) -> list[TrackLlmProposal]:
    themes = (
        "rainy night window",
        "golden hour dusk",
        "soft snowfall evening",
        "late-night cafe booth",
        "morning coffee desk",
        "quiet afternoon loft",
    )
    out: list[TrackLlmProposal] = []
    for i, theme in enumerate(themes):
        out.append(
            TrackLlmProposal(
                position=i,
                subtheme=theme,
                instruments=f"instruments set {i}",
                arrangement=f"arrangement development {i}",
                texture=f"texture energy {i}",
                prompt_ingredients=[theme, "dusty vinyl"],
                desired_bpm=(bpm_by_position or {}).get(i, desired_bpm),
                desired_music_key=(key_by_position or {}).get(i, desired_music_key),
            )
        )
    return out


def _daily_post(*, tracks: list[TrackLlmProposal] | None = None) -> DailyPost:
    return DailyPost(
        genre=_GENRE,
        mood="calm and warm late-night study vibe",
        bpm_range=(70, 90),
        visual_direction=_VISUAL,
        title_directive="lofi study beats",
        description_directive="relaxing lo-fi hip hop for focus and study sessions",
        tracks=tracks,
    )


def test_hash_and_seed_reproducible() -> None:
    plan_id = uuid.uuid4()
    post = _daily_post(tracks=_proposals())
    a = compile_post(
        plan_id=plan_id,
        post_position=0,
        daily_post=post,
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    b = compile_post(
        plan_id=plan_id,
        post_position=0,
        daily_post=post,
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    assert a.compilation_hash == b.compilation_hash
    assert a.source_fingerprint == b.source_fingerprint
    assert [t.system_locked.seed for t in a.tracks] == [
        t.system_locked.seed for t in b.tracks
    ]


def test_desired_equals_final_no_clamp() -> None:
    batch = compile_post(
        plan_id="plan-a",
        post_position=0,
        daily_post=_daily_post(tracks=_proposals(desired_bpm=80)),
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    for track in batch.tracks:
        assert track.system_locked.final_bpm == 80
        assert track.system_locked.bpm_adjustment is None
        assert track.llm_proposal.desired_bpm == 80


def test_bpm_clamp_boundaries_and_reason() -> None:
    # below min
    low = compile_post(
        plan_id="plan-b",
        post_position=0,
        daily_post=_daily_post(tracks=_proposals(desired_bpm=60)),
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    adj_low = low.tracks[0].system_locked.bpm_adjustment
    assert low.tracks[0].system_locked.final_bpm == 70
    assert adj_low is not None
    assert adj_low.desired_bpm == 60
    assert adj_low.final_bpm == 70
    assert adj_low.allowed_min == 70
    assert adj_low.allowed_max == 90
    assert "allowed_min" in adj_low.reason

    # above max
    high = compile_post(
        plan_id="plan-c",
        post_position=0,
        daily_post=_daily_post(tracks=_proposals(desired_bpm=120)),
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    adj_high = high.tracks[0].system_locked.bpm_adjustment
    assert high.tracks[0].system_locked.final_bpm == 90
    assert adj_high is not None
    assert adj_high.desired_bpm == 120
    assert adj_high.final_bpm == 90
    assert "allowed_max" in adj_high.reason


def test_invalid_music_key_blocks_finalize() -> None:
    with pytest.raises(MusicCompileError, match="invalid music key"):
        compile_post(
            plan_id="plan-d",
            post_position=0,
            daily_post=_daily_post(
                tracks=_proposals(desired_music_key="H major")  # not in allowlist
            ),
            genre_bpm_min=70,
            genre_bpm_max=90,
        )


def test_runtime_prompt_and_gpu_use_final_bpm_key() -> None:
    batch = compile_post(
        plan_id="plan-e",
        post_position=0,
        daily_post=_daily_post(tracks=_proposals(desired_bpm=55)),
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    locked = batch.tracks[0].system_locked
    proposal = batch.tracks[0].llm_proposal
    assert locked.final_bpm == 70
    assert "70 BPM" in locked.caption
    assert "55 BPM" not in locked.caption
    assert locked.music_key == "C major"
    assert "key C major" in locked.caption
    # ADR-0040: LLM提案の instruments / arrangement / texture を caption に含める
    assert proposal.instruments in locked.caption
    assert proposal.arrangement in locked.caption
    assert proposal.texture in locked.caption
    assert locked.caption.startswith(f"{_GENRE}, {proposal.subtheme}, ")
    assert ", instrumental, 70 BPM, key C major" in locked.caption

    request = music_request_from_execution_spec(
        prompt=locked.caption,
        duration_sec=locked.duration_sec,
        bpm=locked.final_bpm,
        music_key=locked.music_key,
        seed=locked.seed,
        output_uri="file:///tmp/out.wav",
    )
    assert request.bpm == 70
    assert request.music_key == "C major"
    assert request.prompt == locked.caption


def test_six_tracks_unique_prompt_subtheme_seed_duration() -> None:
    batch = compile_post(
        plan_id="plan-f",
        post_position=1,
        daily_post=_daily_post(tracks=_proposals()),
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    assert len(batch.tracks) == TRACK_COUNT
    subthemes = [t.llm_proposal.subtheme for t in batch.tracks]
    captions = [t.system_locked.caption for t in batch.tracks]
    seeds = [t.system_locked.seed for t in batch.tracks]
    assert len(set(subthemes)) == TRACK_COUNT
    assert len(set(captions)) == TRACK_COUNT
    assert len(set(seeds)) == TRACK_COUNT
    assert all(t.system_locked.duration_sec == TRACK_DURATION_SEC for t in batch.tracks)
    assert batch.duration_sec == 300
    assert all(t.system_locked.model == "acestep-1.5" for t in batch.tracks)


def test_no_visual_directive_leakage() -> None:
    batch = compile_post(
        plan_id="plan-g",
        post_position=0,
        daily_post=_daily_post(tracks=_proposals()),
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    for track in batch.tracks:
        assert _VISUAL not in track.system_locked.caption
        assert "UNIQUE_VISUAL_MARKER" not in track.system_locked.caption


def test_preview_matches_gpu_payload_except_output_uri() -> None:
    batch = compile_post(
        plan_id="plan-h",
        post_position=0,
        daily_post=_daily_post(tracks=_proposals()),
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    locked = batch.tracks[3].system_locked
    preview = execution_spec_dict(locked)
    request = music_request_from_execution_spec(
        prompt=locked.caption,
        duration_sec=locked.duration_sec,
        bpm=locked.final_bpm,
        music_key=locked.music_key,
        seed=locked.seed,
        output_uri="file:///srv/ymg/outputs/music/x/track_3.wav",
    )
    payload = request.model_dump(mode="json", exclude_none=True)
    for key, value in preview.items():
        assert payload[key] == value
    assert payload["output_uri"].endswith("track_3.wav")


def test_compiler_version_change_invalidates_hash() -> None:
    post = _daily_post(tracks=_proposals())
    base = compile_post(
        plan_id="plan-i",
        post_position=0,
        daily_post=post,
        genre_bpm_min=70,
        genre_bpm_max=90,
        compiler_version=COMPILER_VERSION,
    )
    bumped = compile_post(
        plan_id="plan-i",
        post_position=0,
        daily_post=post,
        genre_bpm_min=70,
        genre_bpm_max=90,
        compiler_version="music_compiler_v3",
    )
    assert base.compilation_hash != bumped.compilation_hash
    assert [t.system_locked.seed for t in base.tracks] != [
        t.system_locked.seed for t in bumped.tracks
    ]
    assert COMPILER_VERSION == "music_compiler_v2"


def test_legacy_plan_expands_genre_template() -> None:
    """tracks 欠落の legacy Plan は genre template から 6 本展開する。"""
    batch = compile_post(
        plan_id="plan-legacy",
        post_position=0,
        daily_post=_daily_post(tracks=None),
        genre_bpm_min=70,
        genre_bpm_max=90,
    )
    assert len(batch.tracks) == 6
    assert len({t.llm_proposal.subtheme for t in batch.tracks}) == 6
    # bpm_range midpoint 80 is within genre range → no clamp
    assert all(t.system_locked.final_bpm == 80 for t in batch.tracks)
    # template 由来の instruments/arrangement/texture も caption に入る
    for track in batch.tracks:
        assert track.llm_proposal.instruments in track.system_locked.caption
        assert track.llm_proposal.arrangement in track.system_locked.caption
        assert track.llm_proposal.texture in track.system_locked.caption


def test_build_runtime_prompt_skips_blank_optional_fields() -> None:
    from ymg_backend.domain.pipeline.music_compiler import build_runtime_prompt

    thin = build_runtime_prompt(
        genre="ambient",
        subtheme="deep ocean drift",
        final_bpm=70,
        music_key="A minor",
        instruments="  ",
        arrangement=None,
        texture="",
    )
    assert thin == "ambient, deep ocean drift, instrumental, 70 BPM, key A minor"
