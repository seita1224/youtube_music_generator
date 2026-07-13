"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Music2 } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  approvePlan,
  getPlan,
  type Plan,
  type PlanCycle,
  type PlanStatus,
} from "@/lib/api/plans";
import {
  downloadPostTrack,
  listPosts,
  listPostTracks,
  postTrackAudioPath,
  type AcoustidStatus,
  type AudioTrack,
  type Post,
  type PostStatus,
} from "@/lib/api/posts";
import { useAuthedBlobUrl } from "@/lib/api/use-authed-blob-url";

// Plan 詳細画面(screen-spec.md §2 ③)。
// メタ + 承認 + Post 別 6 トラックの認証付き再生/ダウンロード。
// audio_uri は画面に出さず、 useAuthedBlobUrl / downloadPostTrack 経由。

const CYCLE_LABELS: Readonly<Record<PlanCycle, string>> = {
  daily: "日次",
  weekly: "週次",
};

const STATUS_LABELS: Readonly<Record<PlanStatus, string>> = {
  generated: "生成済",
  approved: "承認済",
  executing: "実行中",
  music_generated: "音楽生成済",
  completed: "完了",
  failed: "失敗",
};

const STATUS_BADGE_VARIANT: Readonly<
  Record<PlanStatus, "default" | "active" | "danger" | "muted">
> = {
  generated: "active",
  approved: "muted",
  executing: "default",
  music_generated: "default",
  completed: "default",
  failed: "danger",
};

const POST_STATUS_LABELS: Readonly<Record<PostStatus, string>> = {
  pending: "待機",
  generating: "生成中",
  music_generated: "音楽生成済",
  generated: "生成済",
  posting: "投稿中",
  posted: "投稿済",
  failed: "失敗",
};

const ACOUSTID_LABELS: Readonly<Record<AcoustidStatus, string>> = {
  not_checked: "未検査",
  clear: "CLEAR",
  hit: "HIT",
  api_error: "APIエラー",
};

function formatDate(value: string | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function formatDateTime(value: string | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function readString(payload: Record<string, unknown> | undefined, key: string): string | null {
  if (!payload) {
    return null;
  }
  const value = payload[key];
  return typeof value === "string" ? value : null;
}

export default function PlanDetailPage(): React.JSX.Element {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const queryClient = useQueryClient();

  const planQuery = useQuery({
    queryKey: ["plans", id],
    queryFn: () => getPlan(id),
  });

  const postsQuery = useQuery({
    queryKey: ["posts", "plan", id],
    queryFn: () => listPosts(id),
    enabled: planQuery.isSuccess,
  });

  const approveMutation = useMutation({
    mutationFn: () => approvePlan(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["plans", id] });
      void queryClient.invalidateQueries({ queryKey: ["plans"] });
    },
  });

  if (planQuery.isLoading) {
    return (
      <div className="flex flex-col gap-6">
        <p className="text-sm text-slate-500">読み込み中…</p>
      </div>
    );
  }

  if (planQuery.isError) {
    return (
      <div className="flex flex-col gap-6">
        <p className="text-sm text-slate-500">
          backend に接続できません(未接続)。
        </p>
        <Link href="/plans" className="text-sm text-primary hover:underline">
          プラン一覧へ戻る
        </Link>
      </div>
    );
  }

  const plan = planQuery.data;
  if (!plan) {
    return (
      <div className="flex flex-col gap-6">
        <p className="text-sm text-slate-500">プランが見つかりません。</p>
        <Link href="/plans" className="text-sm text-primary hover:underline">
          プラン一覧へ戻る
        </Link>
      </div>
    );
  }

  const targetLabel =
    plan.cycle === "weekly"
      ? formatDate(plan.target_week_start)
      : formatDate(plan.target_date);
  const canApprove = plan.status === "generated";
  const posts = postsQuery.data?.items ?? [];

  return (
    <div className="flex flex-col gap-6" data-testid="plan-detail">
      <nav className="text-xs text-slate-500">
        <Link href="/plans" className="text-primary hover:underline">
          プラン
        </Link>
        <span className="mx-1.5">›</span>
        <span>
          {targetLabel} {CYCLE_LABELS[plan.cycle]}
        </span>
      </nav>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <Music2 className="h-5 w-5 text-primary" aria-hidden="true" />
            <h1 className="text-lg font-semibold text-slate-200">
              {targetLabel} {CYCLE_LABELS[plan.cycle]}プラン
            </h1>
            <Badge
              data-testid="plan-detail-status"
              variant={STATUS_BADGE_VARIANT[plan.status]}
            >
              {STATUS_LABELS[plan.status]}
            </Badge>
          </div>
          <p className="text-sm text-slate-400">
            YouTube 視聴維持率の最大化を狙う
            {CYCLE_LABELS[plan.cycle]}自動投稿計画
          </p>
        </div>
        {canApprove ? (
          <Button
            data-testid="plan-detail-approve-btn"
            size="sm"
            disabled={approveMutation.isPending}
            onClick={() => approveMutation.mutate()}
          >
            {approveMutation.isPending ? "承認中…" : "承認"}
          </Button>
        ) : null}
      </div>

      {approveMutation.isError ? (
        <p className="text-sm text-danger">
          承認に失敗しました。 時間をおいて再試行してください。
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>メタ情報</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
            <MetaRow label="サイクル" value={CYCLE_LABELS[plan.cycle]} />
            <MetaRow
              label={plan.cycle === "weekly" ? "対象週" : "対象日"}
              value={targetLabel}
            />
            <MetaRow label="LLM プロバイダ" value={plan.llm_provider} />
            <MetaRow label="モデル" value={plan.llm_model} />
            <MetaRow label="プロンプトバージョン" value={plan.llm_prompt_version} />
            <MetaRow label="コスト" value={`$${plan.llm_cost_usd}`} />
            <MetaRow label="生成日時" value={formatDateTime(plan.created_at)} />
            <MetaRow label="承認日時" value={formatDateTime(plan.approved_at)} />
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>立案理由(Rationale)</CardTitle>
        </CardHeader>
        <CardContent>
          <blockquote className="whitespace-pre-wrap border-l-2 border-primary/40 pl-4 text-sm text-slate-200">
            {plan.rationale}
          </blockquote>
        </CardContent>
      </Card>

      <section className="flex flex-col gap-4">
        <h2 className="text-base font-semibold text-slate-200">投稿(Posts)</h2>
        {postsQuery.isLoading ? (
          <p className="text-sm text-slate-500">投稿を読み込み中…</p>
        ) : null}
        {postsQuery.isError ? (
          <p className="text-sm text-slate-500">投稿一覧を取得できません。</p>
        ) : null}
        {!postsQuery.isLoading && !postsQuery.isError && posts.length === 0 ? (
          <p data-testid="plan-posts-empty" className="text-sm text-slate-500">
            まだ投稿がありません。 音楽生成後に表示されます。
          </p>
        ) : null}
        {posts.map((post) => (
          <PostSection key={post.id} post={post} />
        ))}
      </section>

      <ReferencedMetrics payload={plan.payload} />
    </div>
  );
}

function PostSection({ post }: { readonly post: Post }): React.JSX.Element {
  const tracksQuery = useQuery({
    queryKey: ["posts", post.id, "tracks"],
    queryFn: () => listPostTracks(post.id),
  });

  const tracks = tracksQuery.data?.items ?? [];
  const mood = readString(post.payload, "mood");
  const bpmRange = readString(post.payload, "bpm_range");
  const visualDirection = readString(post.payload, "visual_direction");
  const titleDirective = readString(post.payload, "title_directive");
  const descriptionDirective = readString(post.payload, "description_directive");

  return (
    <Card data-testid={`plan-post-${post.id}`}>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-col gap-1">
            <CardTitle className="text-slate-300">
              {post.final_title ?? `Post #${post.position + 1}`}
            </CardTitle>
            <p className="font-mono text-xs text-slate-500">{post.id}</p>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="active">{post.genre}</Badge>
            <Badge variant="muted">
              {POST_STATUS_LABELS[post.status]}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <dl className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
          {mood ? <MetaRow label="mood" value={mood} /> : null}
          {bpmRange ? <MetaRow label="bpm_range" value={bpmRange} /> : null}
          {visualDirection ? (
            <MetaRow label="visual_direction" value={visualDirection} />
          ) : null}
          {titleDirective ? (
            <MetaRow label="title_directive" value={titleDirective} />
          ) : null}
          {descriptionDirective ? (
            <MetaRow label="description_directive" value={descriptionDirective} />
          ) : null}
          {post.scheduled_at ? (
            <MetaRow
              label="投稿予定"
              value={formatDateTime(post.scheduled_at)}
            />
          ) : null}
        </dl>

        <Separator />

        <div className="flex flex-col gap-3">
          <h3 className="text-sm font-medium text-slate-300">音声トラック</h3>
          {tracksQuery.isLoading ? (
            <p className="text-xs text-slate-500">トラックを読み込み中…</p>
          ) : null}
          {tracksQuery.isError ? (
            <p className="text-xs text-danger">トラックの取得に失敗しました。</p>
          ) : null}
          {!tracksQuery.isLoading && !tracksQuery.isError ? (
            <ul className="flex flex-col gap-3">
              {TRACK_POSITIONS.map((position) => {
                const track = tracks.find((item) => item.position === position);
                return (
                  <TrackRow
                    key={position}
                    postId={post.id}
                    position={position}
                    track={track ?? null}
                  />
                );
              })}
            </ul>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

const TRACK_POSITIONS = [0, 1, 2, 3, 4, 5] as const;

function TrackRow({
  postId,
  position,
  track,
}: {
  readonly postId: string;
  readonly position: number;
  readonly track: AudioTrack | null;
}): React.JSX.Element {
  const hasTrack = track != null;
  // WAV は画面表示時に一括取得せず、 ユーザーが再生を要求したときだけ lazy-load する。
  const [playbackRequested, setPlaybackRequested] = React.useState(false);
  const audioSrc = useAuthedBlobUrl(
    hasTrack && playbackRequested ? postTrackAudioPath(postId, position) : null,
  );
  const [downloading, setDownloading] = React.useState(false);
  const [downloadError, setDownloadError] = React.useState(false);

  const handleDownload = async (): Promise<void> => {
    if (!hasTrack) {
      return;
    }
    setDownloading(true);
    setDownloadError(false);
    try {
      await downloadPostTrack(postId, position);
    } catch {
      setDownloadError(true);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <li
      data-testid={`plan-track-${postId}-${position}`}
      data-generated={hasTrack ? "true" : "false"}
      className="flex flex-col gap-2 rounded-md border border-white/10 bg-white/[0.02] p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <div className="flex flex-wrap items-center gap-2 text-slate-300">
          <span className="font-mono">#{position}</span>
          {hasTrack ? (
            <>
              <span>{track.subtheme ?? "—"}</span>
              <span className="text-slate-500">{track.duration_sec}s</span>
              <span className="text-slate-500">
                BPM {track.bpm ?? "—"}
              </span>
              <Badge variant="muted">
                {ACOUSTID_LABELS[track.acoustid_status]}
              </Badge>
            </>
          ) : (
            <span className="text-slate-500">未生成</span>
          )}
        </div>
        <Button
          data-testid={`plan-track-download-${postId}-${position}`}
          variant="outline"
          size="sm"
          disabled={!hasTrack || downloading}
          onClick={() => {
            void handleDownload();
          }}
        >
          <Download className="h-3.5 w-3.5" aria-hidden="true" />
          {downloading ? "取得中…" : "ダウンロード"}
        </Button>
      </div>
      {hasTrack ? (
        playbackRequested ? (
          audioSrc ? (
            <audio
              data-testid={`plan-track-audio-${postId}-${position}`}
              controls
              preload="none"
              src={audioSrc}
              className="w-full"
              autoPlay
            >
              お使いのブラウザは audio 要素に対応していません。
            </audio>
          ) : (
            <p
              data-testid={`plan-track-audio-loading-${postId}-${position}`}
              className="text-xs text-slate-500"
            >
              音声を読み込み中…
            </p>
          )
        ) : (
          <Button
            data-testid={`plan-track-play-${postId}-${position}`}
            variant="outline"
            size="sm"
            className="self-start"
            onClick={() => setPlaybackRequested(true)}
          >
            再生する
          </Button>
        )
      ) : (
        <p className="text-xs text-slate-600">再生・ダウンロードは未生成のため無効です。</p>
      )}
      {downloadError ? (
        <p className="text-xs text-danger">ダウンロードに失敗しました。</p>
      ) : null}
    </li>
  );
}

function ReferencedMetrics({
  payload,
}: {
  readonly payload: Plan["payload"];
}): React.JSX.Element | null {
  const raw = payload["referenced_metrics"];
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const metrics = raw as Record<string, unknown>;
  const windowDays =
    typeof metrics["window_days"] === "number"
      ? String(metrics["window_days"])
      : null;
  const sampleSize =
    typeof metrics["sample_size"] === "number"
      ? String(metrics["sample_size"])
      : null;
  const summary =
    typeof metrics["top_metrics_summary"] === "string"
      ? metrics["top_metrics_summary"]
      : null;
  if (!windowDays && !sampleSize && !summary) {
    return null;
  }
  return (
    <Card data-testid="plan-referenced-metrics">
      <CardHeader>
        <CardTitle>参照メトリクス(Referenced Metrics)</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
          {windowDays ? <MetaRow label="window_days" value={windowDays} /> : null}
          {sampleSize ? <MetaRow label="sample_size" value={sampleSize} /> : null}
          {summary ? (
            <MetaRow label="top_metrics_summary" value={summary} />
          ) : null}
        </dl>
      </CardContent>
    </Card>
  );
}

function MetaRow({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}): React.JSX.Element {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="break-all text-slate-300">{value}</dd>
    </div>
  );
}
