"use client";

import * as React from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  approveDryrun,
  dryrunVideoPath,
  listDryrunOutputs,
  rejectDryrun,
  type DryrunOutput,
  type DryrunState,
} from "@/lib/api/dryrun";
import { useAuthedBlobUrl } from "@/lib/api/use-authed-blob-url";

// T098: dryrun 詳細画面(screen-spec.md §2 / US2)。
// 動画プレビュー + メタ情報 + 承認(承認して投稿)/ 却下(理由必須 min4)操作。
// 単一取得 endpoint は contracts に無いため一覧から id で選び出す(承認/却下/動画は専用 endpoint)。
// 動画は API クライアント経由ではなく同一オリジン proxied パスで再生(Basic 認証セッション利用)。

const REJECT_REASON_MIN_LENGTH = 4;

// state → 日本語ラベル(UI 契約)。 pending=保留中 / approved=承認済 /
// rejected=却下 / auto_expired=期限切れ / posted=投稿済。
const STATE_LABELS: Record<DryrunState, string> = {
  pending: "保留中",
  approved: "承認済",
  rejected: "却下",
  auto_expired: "期限切れ",
  posted: "投稿済",
};

// state → Badge variant。 終端の rejected / auto_expired は danger 寄せ、
// pending は active(要対応)、 投稿済/承認済は default。
const STATE_BADGE_VARIANTS: Record<
  DryrunState,
  "default" | "active" | "danger" | "muted"
> = {
  pending: "active",
  approved: "default",
  rejected: "danger",
  auto_expired: "muted",
  posted: "default",
};

/** ISO 文字列を読みやすい表記に整形する。 未指定/不正値はそのまま「—」。 */
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

export default function DryrunDetailPage(): React.JSX.Element {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const queryClient = useQueryClient();

  const [rejectOpen, setRejectOpen] = React.useState(false);
  const [rejectReason, setRejectReason] = React.useState("");

  // 単一取得 endpoint が無いため一覧を取得し id で抽出する。
  const query = useQuery({
    queryKey: ["dryrun", "outputs"],
    queryFn: () => listDryrunOutputs(),
  });

  const output: DryrunOutput | undefined = query.data?.items.find(
    (item) => item.id === id,
  );

  const invalidate = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: ["dryrun", "outputs"] });

  const approveMutation = useMutation({
    mutationFn: () => approveDryrun(id),
    onSuccess: () => {
      void invalidate();
    },
  });

  const rejectMutation = useMutation({
    mutationFn: (reason: string) => rejectDryrun(id, reason),
    onSuccess: () => {
      setRejectOpen(false);
      setRejectReason("");
      void invalidate();
    },
  });

  const reasonTrimmed = rejectReason.trim();
  const reasonValid = reasonTrimmed.length >= REJECT_REASON_MIN_LENGTH;
  const isPending = output?.state === "pending";
  const mutating = approveMutation.isPending || rejectMutation.isPending;

  // 動画は Basic 認証必須のため authFetch→blob で取得(素の <video src> は 401)。
  // 削除済み state(却下/期限切れ)は配信不可なので取得しない。 hooks 規則のため
  // 早期 return より前で無条件に呼ぶ(path=null の間は取得しない)。
  const videoServable =
    output != null && output.state !== "rejected" && output.state !== "auto_expired";
  const videoSrc = useAuthedBlobUrl(
    videoServable && output != null ? dryrunVideoPath(output.id) : null,
  );

  if (query.isLoading) {
    return (
      <div className="flex flex-col gap-6">
        <p className="text-sm text-slate-500">読み込み中…</p>
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="flex flex-col gap-6">
        <p className="text-sm text-slate-500">
          backend に接続できません(未接続)。
        </p>
      </div>
    );
  }

  if (!output) {
    return (
      <div className="flex flex-col gap-6">
        <Link href="/dryrun" className="text-sm text-primary hover:underline">
          ← Dryrun 審査一覧へ戻る
        </Link>
        <p className="text-sm text-slate-500">
          指定の dryrun 出力が見つかりません(id: {id})。
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-4">
        <Link href="/dryrun" className="text-sm text-primary hover:underline">
          ← Dryrun 審査一覧へ戻る
        </Link>
        <Badge
          data-testid="dryrun-state-badge"
          variant={STATE_BADGE_VARIANTS[output.state]}
        >
          {STATE_LABELS[output.state]}
        </Badge>
      </div>

      {/* タイトル優先表示(UUID より識別しやすい)。 未設定時は出力 ID にフォールバック。 */}
      <h1 className="text-xl font-semibold text-slate-100">
        {output.title ?? output.id}
      </h1>

      <Card>
        <CardHeader>
          <CardTitle>動画プレビュー</CardTitle>
        </CardHeader>
        <CardContent>
          {/* 同一オリジン proxied パスでブラウザの Basic 認証セッションを再利用する
              (authFetch は経由しない)。 rejected / auto_expired は配信不可(409)。 */}
          <video
            data-testid="dryrun-video"
            src={videoSrc ?? undefined}
            controls
            className="w-full aspect-video rounded-lg border border-white/10 bg-black"
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>メタ情報</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
            <MetaRow label="出力 ID" value={output.id} mono />
            <MetaRow label="投稿 ID" value={output.post_id} mono />
            <MetaRow label="状態" value={STATE_LABELS[output.state]} />
            <MetaRow label="動画 URI" value={output.video_uri} mono />
            <MetaRow label="作成日時" value={formatDateTime(output.created_at)} />
            <MetaRow
              label="レビュー日時"
              value={formatDateTime(output.reviewed_at)}
            />
            <MetaRow
              label="投稿日時"
              value={formatDateTime(output.posted_at)}
            />
            <MetaRow
              label="期限切れ日時"
              value={formatDateTime(output.auto_expired_at)}
            />
            {output.reject_reason ? (
              <MetaRow
                label="却下理由"
                value={output.reject_reason}
                className="sm:col-span-2"
              />
            ) : null}
          </dl>
        </CardContent>
      </Card>

      {/* 操作は pending のみ可。 終端状態(approved/posted/rejected/auto_expired)は非表示。 */}
      {isPending ? (
        <Card>
          <CardHeader>
            <CardTitle>審査操作</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-col gap-3 sm:flex-row">
              <Button
                data-testid="dryrun-approve-btn"
                variant="default"
                disabled={mutating}
                onClick={() => approveMutation.mutate()}
              >
                承認して投稿
              </Button>
              <Button
                data-testid="dryrun-reject-btn"
                variant="danger"
                disabled={mutating}
                onClick={() => setRejectOpen(true)}
              >
                却下
              </Button>
            </div>
            {approveMutation.isError ? (
              <p className="text-sm text-danger">
                承認に失敗しました。 時間をおいて再試行してください。
              </p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <Dialog open={rejectOpen} onOpenChange={(open) => setRejectOpen(open)}>
        <DialogHeader>
          <DialogTitle>却下理由を入力</DialogTitle>
          <DialogDescription>
            却下理由は {REJECT_REASON_MIN_LENGTH} 文字以上で入力してください。
            次回のプラン生成で同様の方向性を避ける手がかりになります。
          </DialogDescription>
        </DialogHeader>
        <DialogContent>
          <Textarea
            data-testid="dryrun-reject-reason"
            value={rejectReason}
            onChange={(event) => setRejectReason(event.target.value)}
            placeholder="例: 既存曲と雰囲気が酷似しているため"
            rows={4}
          />
          {!reasonValid && reasonTrimmed.length > 0 ? (
            <p className="text-xs text-danger">
              却下理由は {REJECT_REASON_MIN_LENGTH} 文字以上で入力してください。
            </p>
          ) : null}
          {rejectMutation.isError ? (
            <p className="text-xs text-danger">
              却下に失敗しました。 時間をおいて再試行してください。
            </p>
          ) : null}
        </DialogContent>
        <Separator />
        <DialogFooter>
          <Button
            variant="outline"
            disabled={rejectMutation.isPending}
            onClick={() => setRejectOpen(false)}
          >
            キャンセル
          </Button>
          <Button
            data-testid="dryrun-reject-confirm-btn"
            variant="danger"
            disabled={!reasonValid || rejectMutation.isPending}
            onClick={() => rejectMutation.mutate(reasonTrimmed)}
          >
            却下を確定
          </Button>
        </DialogFooter>
      </Dialog>
    </div>
  );
}

function MetaRow({
  label,
  value,
  mono = false,
  className,
}: {
  readonly label: string;
  readonly value: string;
  readonly mono?: boolean;
  readonly className?: string;
}): React.JSX.Element {
  return (
    <div className={["flex flex-col gap-0.5", className].filter(Boolean).join(" ")}>
      <dt className="text-slate-500">{label}</dt>
      <dd
        className={
          mono
            ? "break-all font-mono text-slate-200"
            : "text-slate-200"
        }
      >
        {value}
      </dd>
    </div>
  );
}
