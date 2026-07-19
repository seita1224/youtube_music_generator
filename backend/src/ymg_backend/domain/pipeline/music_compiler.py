"""実行用音楽生成仕様の決定論 compiler (ADR-0032 / ADR-0039 / U3)。

LLM提案(desired BPM/key・subtheme 等)を versioned policy で検証し、システム確定層
(最終 BPM・music key・実行時プロンプト・duration=300・model・seed)を固定する。

- BPM は許容範囲へ clamp 可能(入力・最終・範囲・理由を保存)
- music key が無効なら暗黙補正せず :class:`MusicCompileError` (仕様を確定しない)
- 実行時プロンプトは最終 BPM / music key / subtheme に加え、LLM提案の
  instruments / arrangement / texture が非空なら決定論的に連結する
  (``visual_direction`` は混ぜない)
- legacy Plan(``tracks`` 欠落)は versioned genre template から決定論的に 6 本展開
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from ymg_backend.domain.plans.schemas import DailyPost, TrackLlmProposal

# --- versioned policy ----------------------------------------------------------

# v2: caption に instruments / arrangement / texture を決定論連結(ADR-0040)。
COMPILER_VERSION: Final[str] = "music_compiler_v2"
MUSIC_MODEL: Final[str] = "acestep-1.5"
TRACK_DURATION_SEC: Final[int] = 300
TRACK_COUNT: Final[int] = 6

# ACE-Step / MusicGenerateRequest 契約 (gpu-worker-api.yaml)。
_WORKER_BPM_MIN: Final[int] = 50
_WORKER_BPM_MAX: Final[int] = 200

_NOTE_NAMES: Final[tuple[str, ...]] = (
    "C",
    "C#",
    "Db",
    "D",
    "D#",
    "Eb",
    "E",
    "F",
    "F#",
    "Gb",
    "G",
    "G#",
    "Ab",
    "A",
    "A#",
    "Bb",
    "B",
)
_MODES: Final[tuple[str, ...]] = ("major", "minor")
ALLOWED_MUSIC_KEYS: Final[frozenset[str]] = frozenset(
    f"{note} {mode}" for note in _NOTE_NAMES for mode in _MODES
)

# genre template v1: legacy Plan 用。各ジャンル 6 つの一意 subtheme (ADR-0003 例示に整合)。
_GENRE_SUBTHEME_TEMPLATES_V1: Final[dict[str, tuple[str, ...]]] = {
    "lo-fi hip-hop": (
        "rainy night window",
        "golden hour dusk",
        "soft snowfall evening",
        "late-night cafe booth",
        "morning coffee desk",
        "quiet afternoon loft",
    ),
    "chillhop": (
        "vinyl crackle lounge",
        "jazz club after hours",
        "riverside walk dusk",
        "bookshelf reading nook",
        "subway ride twilight",
        "rooftop breeze night",
    ),
    "ambient": (
        "deep ocean drift",
        "fog over marshland",
        "starfield slow pulse",
        "empty cathedral reverb",
        "polar night horizon",
        "wind through pines",
    ),
    "synthwave": (
        "neon freeway night",
        "arcade lobby glow",
        "chrome skyline drive",
        "retro mall sunset",
        "laser grid chase",
        "coastal synth dusk",
    ),
    "piano solo": (
        "rainy conservatory",
        "candlelit etude",
        "dawn practice room",
        "empty recital hall",
        "winter window seat",
        "late study sonata",
    ),
    "future garage": (
        "misty docklands 2am",
        "halftime city rain",
        "neon underpass pulse",
        "distant train haze",
        "glass tower afterglow",
        "wet pavement shuffle",
    ),
}

_DEFAULT_SUBTHEMES_V1: Final[tuple[str, ...]] = (
    "opening motif",
    "rising midsection",
    "textural bridge",
    "peak energy drop",
    "reflective breakdown",
    "closing resolve",
)

_GENRE_DEFAULT_KEYS_V1: Final[dict[str, str]] = {
    "lo-fi hip-hop": "C major",
    "chillhop": "F major",
    "ambient": "A minor",
    "synthwave": "E minor",
    "piano solo": "D major",
    "future garage": "G minor",
}

_TEMPLATE_INSTRUMENTS_V1: Final[tuple[str, ...]] = (
    "warm rhodes, soft kick, vinyl dust",
    "muted guitar, brushed snare, sub bass",
    "pad wash, sparse percussion, field noise",
    "arpeggiated synth, sidechain bass, clap",
    "felt piano, soft strings, gentle hats",
    "chopped samples, deep kick, airy vocals",
)

_TEMPLATE_ARRANGEMENTS_V1: Final[tuple[str, ...]] = (
    "intro pad into main groove, gradual layering",
    "verse-loop with filter opens, mid drop",
    "ambient bed then rhythmic entry, long tails",
    "driving pulse with breakdown at midpoint",
    "minimal motif, expanding harmony late",
    "call-and-response then soft outro fade",
)

_TEMPLATE_TEXTURES_V1: Final[tuple[str, ...]] = (
    "low energy, intimate, lo-fi warmth",
    "medium energy, dusty, mid-forward",
    "soft dynamics, wide stereo air",
    "higher energy, punchy transient focus",
    "restrained energy, dark low end",
    "gentle lift then calm resolve",
)

__all__ = [
    "ALLOWED_MUSIC_KEYS",
    "COMPILER_VERSION",
    "CompiledMusicBatch",
    "MUSIC_MODEL",
    "MusicCompileError",
    "SystemLockedTrack",
    "TRACK_COUNT",
    "TRACK_DURATION_SEC",
    "TrackRecipe",
    "build_runtime_prompt",
    "compile_post",
    "execution_spec_dict",
    "hash_compilation",
]


class MusicCompileError(ValueError):
    """仕様確定不能(無効 key・差分化不足など)。検証 fail 相当。"""


class BpmAdjustment(BaseModel):
    """desired → final の BPM 補正 provenance。"""

    model_config = ConfigDict(frozen=True)

    desired_bpm: int
    final_bpm: int
    allowed_min: int
    allowed_max: int
    reason: str


class SystemLockedTrack(BaseModel):
    """システム確定層(実行に使う値のみ)。"""

    model_config = ConfigDict(frozen=True)

    final_bpm: int = Field(ge=_WORKER_BPM_MIN, le=_WORKER_BPM_MAX)
    music_key: str
    caption: str = Field(min_length=8)  # 実行時プロンプト
    duration_sec: int = Field(default=TRACK_DURATION_SEC)
    model: str = Field(default=MUSIC_MODEL)
    seed: int
    output_position: int = Field(ge=0, le=5)
    compiler_version: str
    bpm_adjustment: BpmAdjustment | None = None


class TrackRecipe(BaseModel):
    """トラック生成レシピ = LLM提案 + システム確定。"""

    model_config = ConfigDict(frozen=True)

    position: int = Field(ge=0, le=5)
    llm_proposal: TrackLlmProposal
    system_locked: SystemLockedTrack


class CompiledMusicBatch(BaseModel):
    """Post position 単位の実行用音楽生成仕様(CompiledMusicBatch)。"""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    post_position: int = Field(ge=0)
    compiler_version: str
    model: str = MUSIC_MODEL
    duration_sec: int = TRACK_DURATION_SEC
    tracks: list[TrackRecipe] = Field(min_length=TRACK_COUNT, max_length=TRACK_COUNT)
    compilation_hash: str
    source_fingerprint: str

    def tracks_json(self) -> list[dict[str, Any]]:
        """``music_compilations.tracks_json`` 用の JSON 互換 list。"""
        return [t.model_dump(mode="json") for t in self.tracks]


def build_runtime_prompt(
    *,
    genre: str,
    subtheme: str,
    final_bpm: int,
    music_key: str,
    instruments: str | None = None,
    arrangement: str | None = None,
    texture: str | None = None,
) -> str:
    """最終 BPM / music key / subtheme (+ 任意の LLM提案フィールド)から実行時プロンプトを合成する。

    ``instruments`` / ``arrangement`` / ``texture`` は strip 後に非空なら、
    genre・subtheme の直後へ決定論順で連結する。``visual_direction`` や画像用指示は含めない。
    """
    parts: list[str] = [genre.strip(), subtheme.strip()]
    for optional in (instruments, arrangement, texture):
        if optional is None:
            continue
        cleaned = optional.strip()
        if cleaned:
            parts.append(cleaned)
    parts.append("instrumental")
    parts.append(f"{final_bpm} BPM")
    parts.append(f"key {music_key}")
    return ", ".join(parts)


def execution_spec_dict(locked: SystemLockedTrack) -> dict[str, Any]:
    """GPU 投入と照合する実行用音楽生成仕様( output_uri 除く )。

    ``gpu_jobs.request_payload`` はこれに ``output_uri`` のみを加えたものと一致させる。
    """
    return {
        "prompt": locked.caption,
        "duration_sec": locked.duration_sec,
        "bpm": locked.final_bpm,
        "music_key": locked.music_key,
        "seed": locked.seed,
    }


def compile_post(
    *,
    plan_id: str | uuid.UUID,
    post_position: int,
    daily_post: DailyPost,
    genre_bpm_min: int | None = None,
    genre_bpm_max: int | None = None,
    compiler_version: str = COMPILER_VERSION,
) -> CompiledMusicBatch:
    """DailyPost から 6 トラックの実行用音楽生成仕様を決定論的に確定する。

    Raises:
        MusicCompileError: 無効 music key、差分化不足、BPM 解決不能など。
    """
    plan_id_str = str(plan_id)
    proposals = _resolve_proposals(
        daily_post=daily_post,
        genre_bpm_min=genre_bpm_min,
        genre_bpm_max=genre_bpm_max,
    )
    allowed_min, allowed_max = _resolve_bpm_bounds(genre_bpm_min, genre_bpm_max)

    recipes: list[TrackRecipe] = []
    for proposal in proposals:
        music_key = _normalize_music_key(proposal.desired_music_key)
        if music_key not in ALLOWED_MUSIC_KEYS:
            raise MusicCompileError(
                f"invalid music key: {proposal.desired_music_key!r} "
                f"(position={proposal.position})"
            )
        final_bpm, adjustment = _clamp_bpm(
            desired_bpm=proposal.desired_bpm,
            allowed_min=allowed_min,
            allowed_max=allowed_max,
        )
        caption = build_runtime_prompt(
            genre=daily_post.genre,
            subtheme=proposal.subtheme,
            final_bpm=final_bpm,
            music_key=music_key,
            instruments=proposal.instruments,
            arrangement=proposal.arrangement,
            texture=proposal.texture,
        )

        seed = _deterministic_seed(
            plan_id=plan_id_str,
            post_position=post_position,
            track_position=proposal.position,
            compiler_version=compiler_version,
        )
        locked = SystemLockedTrack(
            final_bpm=final_bpm,
            music_key=music_key,
            caption=caption,
            duration_sec=TRACK_DURATION_SEC,
            model=MUSIC_MODEL,
            seed=seed,
            output_position=proposal.position,
            compiler_version=compiler_version,
            bpm_adjustment=adjustment,
        )
        recipes.append(
            TrackRecipe(position=proposal.position, llm_proposal=proposal, system_locked=locked)
        )

    _assert_differentiation(recipes)

    source_fingerprint = _source_fingerprint(
        plan_id=plan_id_str,
        post_position=post_position,
        daily_post=daily_post,
        proposals=proposals,
        genre_bpm_min=genre_bpm_min,
        genre_bpm_max=genre_bpm_max,
        compiler_version=compiler_version,
    )
    compilation_hash = hash_compilation(
        plan_id=plan_id_str,
        post_position=post_position,
        compiler_version=compiler_version,
        model=MUSIC_MODEL,
        duration_sec=TRACK_DURATION_SEC,
        tracks=recipes,
        source_fingerprint=source_fingerprint,
    )
    return CompiledMusicBatch(
        plan_id=plan_id_str,
        post_position=post_position,
        compiler_version=compiler_version,
        model=MUSIC_MODEL,
        duration_sec=TRACK_DURATION_SEC,
        tracks=recipes,
        compilation_hash=compilation_hash,
        source_fingerprint=source_fingerprint,
    )


def hash_compilation(
    *,
    plan_id: str,
    post_position: int,
    compiler_version: str,
    model: str,
    duration_sec: int,
    tracks: list[TrackRecipe],
    source_fingerprint: str,
) -> str:
    """実行用仕様の canonical hash (compiler version 変更で無効化される)。"""
    payload = {
        "plan_id": plan_id,
        "post_position": post_position,
        "compiler_version": compiler_version,
        "model": model,
        "duration_sec": duration_sec,
        "source_fingerprint": source_fingerprint,
        "tracks": [
            {
                "position": t.position,
                "llm_proposal": t.llm_proposal.model_dump(mode="json"),
                "system_locked": t.system_locked.model_dump(mode="json"),
            }
            for t in tracks
        ],
    }
    return _stable_hash(payload)


def _resolve_proposals(
    *,
    daily_post: DailyPost,
    genre_bpm_min: int | None,
    genre_bpm_max: int | None,
) -> list[TrackLlmProposal]:
    if daily_post.tracks is not None:
        return list(daily_post.tracks)
    return _expand_legacy_proposals(
        daily_post=daily_post,
        genre_bpm_min=genre_bpm_min,
        genre_bpm_max=genre_bpm_max,
    )


def _expand_legacy_proposals(
    *,
    daily_post: DailyPost,
    genre_bpm_min: int | None,
    genre_bpm_max: int | None,
) -> list[TrackLlmProposal]:
    """versioned genre template から 6 TrackLlmProposal を決定論展開する。"""
    subthemes = _GENRE_SUBTHEME_TEMPLATES_V1.get(daily_post.genre, _DEFAULT_SUBTHEMES_V1)
    if len(subthemes) != TRACK_COUNT:
        raise MusicCompileError(
            f"genre template for {daily_post.genre!r} must have {TRACK_COUNT} subthemes"
        )
    default_key = _GENRE_DEFAULT_KEYS_V1.get(daily_post.genre, "C major")
    desired_bpm = _legacy_desired_bpm(daily_post, genre_bpm_min, genre_bpm_max)
    proposals: list[TrackLlmProposal] = []
    for position, subtheme in enumerate(subthemes):
        proposals.append(
            TrackLlmProposal(
                position=position,
                subtheme=subtheme,
                instruments=_TEMPLATE_INSTRUMENTS_V1[position],
                arrangement=_TEMPLATE_ARRANGEMENTS_V1[position],
                texture=_TEMPLATE_TEXTURES_V1[position],
                prompt_ingredients=[daily_post.genre, subtheme, daily_post.mood],
                desired_bpm=desired_bpm,
                desired_music_key=default_key,
            )
        )
    return proposals


def _legacy_desired_bpm(
    daily_post: DailyPost,
    genre_bpm_min: int | None,
    genre_bpm_max: int | None,
) -> int:
    if daily_post.bpm_range is not None:
        low, high = daily_post.bpm_range
        return (low + high) // 2
    if genre_bpm_min is not None and genre_bpm_max is not None:
        return (genre_bpm_min + genre_bpm_max) // 2
    if genre_bpm_min is not None:
        return genre_bpm_min
    if genre_bpm_max is not None:
        return genre_bpm_max
    return 80


def _resolve_bpm_bounds(
    genre_bpm_min: int | None, genre_bpm_max: int | None
) -> tuple[int, int]:
    low = genre_bpm_min if genre_bpm_min is not None else _WORKER_BPM_MIN
    high = genre_bpm_max if genre_bpm_max is not None else _WORKER_BPM_MAX
    low = max(_WORKER_BPM_MIN, low)
    high = min(_WORKER_BPM_MAX, high)
    if low > high:
        raise MusicCompileError(f"invalid BPM allowed range: [{low}, {high}]")
    return low, high


def _clamp_bpm(
    *, desired_bpm: int, allowed_min: int, allowed_max: int
) -> tuple[int, BpmAdjustment | None]:
    if desired_bpm < _WORKER_BPM_MIN or desired_bpm > _WORKER_BPM_MAX:
        # schema でも 50..200 だが、legacy / 直接呼び出しに備える。
        clamped = min(max(desired_bpm, allowed_min), allowed_max)
        clamped = min(max(clamped, _WORKER_BPM_MIN), _WORKER_BPM_MAX)
        reason = (
            f"desired_bpm {desired_bpm} outside worker range "
            f"[{_WORKER_BPM_MIN}, {_WORKER_BPM_MAX}]; "
            f"clamped into allowed [{allowed_min}, {allowed_max}]"
        )
        return clamped, BpmAdjustment(
            desired_bpm=desired_bpm,
            final_bpm=clamped,
            allowed_min=allowed_min,
            allowed_max=allowed_max,
            reason=reason,
        )
    if allowed_min <= desired_bpm <= allowed_max:
        return desired_bpm, None
    if desired_bpm < allowed_min:
        final = allowed_min
        reason = (
            f"desired_bpm {desired_bpm} below allowed_min {allowed_min}; "
            f"clamped to allowed_min"
        )
    else:
        final = allowed_max
        reason = (
            f"desired_bpm {desired_bpm} above allowed_max {allowed_max}; "
            f"clamped to allowed_max"
        )
    return final, BpmAdjustment(
        desired_bpm=desired_bpm,
        final_bpm=final,
        allowed_min=allowed_min,
        allowed_max=allowed_max,
        reason=reason,
    )


def _normalize_music_key(raw: str) -> str:
    """空白正規化のみ。表記ゆれの暗黙補正はしない(無効なら fail)。"""
    parts = raw.strip().split()
    if len(parts) != 2:
        return raw.strip()
    note, mode = parts[0], parts[1].lower()
    return f"{note} {mode}"


def _deterministic_seed(
    *,
    plan_id: str,
    post_position: int,
    track_position: int,
    compiler_version: str,
) -> int:
    material = f"{plan_id}|{post_position}|{track_position}|{compiler_version}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    # 符号付き 31-bit に収め、 GPU / JSON 互換の正の int にする。
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _assert_differentiation(recipes: list[TrackRecipe]) -> None:
    subthemes = [t.llm_proposal.subtheme for t in recipes]
    captions = [t.system_locked.caption for t in recipes]
    seeds = [t.system_locked.seed for t in recipes]
    if len(set(subthemes)) != TRACK_COUNT:
        raise MusicCompileError("subthemes must be unique across 6 tracks")
    if len(set(captions)) != TRACK_COUNT:
        raise MusicCompileError("runtime prompts must be unique across 6 tracks")
    if len(set(seeds)) != TRACK_COUNT:
        raise MusicCompileError("seeds must be unique across 6 tracks")
    for recipe in recipes:
        if recipe.system_locked.duration_sec != TRACK_DURATION_SEC:
            raise MusicCompileError("duration_sec must be 300")
        if recipe.system_locked.model != MUSIC_MODEL:
            raise MusicCompileError(f"model must be {MUSIC_MODEL}")


def _source_fingerprint(
    *,
    plan_id: str,
    post_position: int,
    daily_post: DailyPost,
    proposals: list[TrackLlmProposal],
    genre_bpm_min: int | None,
    genre_bpm_max: int | None,
    compiler_version: str,
) -> str:
    payload = {
        "plan_id": plan_id,
        "post_position": post_position,
        "compiler_version": compiler_version,
        "genre": daily_post.genre,
        "mood": daily_post.mood,
        "bpm_range": daily_post.bpm_range,
        "genre_bpm_min": genre_bpm_min,
        "genre_bpm_max": genre_bpm_max,
        # visual_direction は fingerprint に含める(入力 provenance)が、実行プロンプトには使わない。
        "visual_direction": daily_post.visual_direction,
        "proposals": [p.model_dump(mode="json") for p in proposals],
    }
    return _stable_hash(payload)


def _stable_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
