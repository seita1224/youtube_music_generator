"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, OctagonX } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  getSchedulerState,
  panicStop,
  setMode,
  setScheduler,
  type PanicStopResult,
  type SchedulerState,
} from "@/lib/api/scheduler";

// US4: スケジューラ運用画面(screen-spec.md / ADR-0031)。
// scheduler 有効/無効トグル(setScheduler)、 dryrun↔投稿モード切替(setMode)、
// コンプラ緊急停止(panic-stop): window_hours を指定して直近動画を private 化する。
// backend `GET /scheduler/mode` は無いため mode はトグル操作の戻り値で楽観反映する。
// backend 未接続時はクエリ失敗を握り潰さず「未接続」として明示する(dryrun と同方針)。

const SCHEDULER_QUERY_KEY = ["scheduler"] as const;
const DEFAULT_WINDOW_HOURS = 24;
const MIN_WINDOW_HOURS = 1;

const PRIVACY_LABELS: Readonly<
  Record<PanicStopResult["recent_videos"][number]["privacy_status"], string>
> = {
  public: "公開",
  unlisted: "限定公開",
  private: "非公開",
  deleted: "削除済",
};

/** ISO 文字列を `YYYY-MM-DD HH:mm` 風に整形する。 未指定/不正値は「—」。 */
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

export default function SchedulerPage(): React.JSX.Element {
  const queryClient = useQueryClient();

  const [windowHoursInput, setWindowHoursInput] = React.useState(
    String(DEFAULT_WINDOW_HOURS),
  );
  const [dialogOpen, setDialogOpen] = React.useState(false);
  // dryrun 状態は GET 不可のため、 トグルの戻り値で楽観反映する(初期は不明)。
  const [dryrunEnabled, setDryrunEnabled] = React.useState<boolean | null>(null);
  const [panicResult, setPanicResult] = React.useState<PanicStopResult | null>(null);

  const query = useQuery<SchedulerState>({
    queryKey: SCHEDULER_QUERY_KEY,
    queryFn: getSchedulerState,
  });

  const invalidate = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: SCHEDULER_QUERY_KEY });

  const enabled = query.data?.enabled ?? false;

  const schedulerMutation = useMutation<SchedulerState, Error, boolean>({
    mutationFn: (next) => setScheduler(next),
    onSuccess: () => {
      void invalidate();
    },
  });

  const modeMutation = useMutation<{ dryrun_enabled: boolean }, Error, boolean>({
    mutationFn: (next) => setMode(next),
    onSuccess: (data) => {
      setDryrunEnabled(data.dryrun_enabled);
    },
  });

  const panicMutation = useMutation<PanicStopResult, Error, number>({
    mutationFn: (hours) => panicStop(hours),
    onSuccess: (result) => {
      setPanicResult(result);
      setDialogOpen(false);
      void invalidate();
    },
  });

  const parsedWindowHours = Number.parseInt(windowHoursInput, 10);
  const windowHoursValid =
    Number.isInteger(parsedWindowHours) && parsedWindowHours >= MIN_WINDOW_HOURS;

  const mutating =
    schedulerMutation.isPending ||
    modeMutation.isPending ||
    panicMutation.isPending;

  const nextDryrun = dryrunEnabled === null ? true : !dryrunEnabled;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <CalendarClock className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">スケジューラ</h1>
      </div>

      {query.isLoading && (
        <p className="text-sm text-slate-500">読み込み中…</p>
      )}

      {query.isError && (
        <p className="text-sm text-slate-500">
          backend に接続できません(未接続)。
        </p>
      )}

      {!query.isLoading && !query.isError && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>稼働状態</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="flex items-center justify-between gap-4">
                <div className="flex flex-col gap-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-slate-300">
                      スケジューラ
                    </span>
                    <Badge variant={enabled ? "active" : "muted"}>
                      {enabled ? "有効" : "無効"}
                    </Badge>
                  </div>
                  <span className="font-mono text-xs text-slate-500">
                    更新: {formatDateTime(query.data?.updated_at)}
                  </span>
                </div>
                <Button
                  data-testid="scheduler-enabled-toggle"
                  variant={enabled ? "outline" : "default"}
                  disabled={mutating}
                  onClick={() => schedulerMutation.mutate(!enabled)}
                >
                  {enabled ? "無効にする" : "有効にする"}
                </Button>
              </div>

              <Separator />

              <div className="flex items-center justify-between gap-4">
                <div className="flex flex-col gap-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-slate-300">投稿モード</span>
                    <Badge
                      variant={
                        dryrunEnabled === null
                          ? "muted"
                          : dryrunEnabled
                            ? "active"
                            : "default"
                      }
                    >
                      {dryrunEnabled === null
                        ? "不明"
                        : dryrunEnabled
                          ? "Dryrun"
                          : "本番投稿"}
                    </Badge>
                  </div>
                  <span className="text-xs text-slate-500">
                    Dryrun は審査用の試走、 本番投稿は YouTube へ実投稿します。
                  </span>
                </div>
                <Button
                  data-testid="scheduler-mode-toggle"
                  variant="outline"
                  disabled={mutating}
                  onClick={() => modeMutation.mutate(nextDryrun)}
                >
                  {nextDryrun ? "Dryrun に切替" : "本番投稿に切替"}
                </Button>
              </div>

              {schedulerMutation.isError ? (
                <p className="text-sm text-danger">
                  スケジューラ状態の更新に失敗しました。 時間をおいて再試行してください。
                </p>
              ) : null}
              {modeMutation.isError ? (
                <p className="text-sm text-danger">
                  モード切替に失敗しました。 時間をおいて再試行してください。
                </p>
              ) : null}
            </CardContent>
          </Card>

          <Card className="border-danger/30">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-danger">
                <OctagonX className="h-4 w-4" aria-hidden="true" />
                コンプラ緊急停止
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <p className="text-sm text-slate-400">
                スケジューラを停止し、 指定時間内に投稿した動画を private 化します。
                重大なコンプラ事象が疑われる場合のみ使用してください。
              </p>
              <div>
                <Button
                  data-testid="panic-stop-btn"
                  variant="danger"
                  disabled={mutating}
                  onClick={() => setDialogOpen(true)}
                >
                  緊急停止
                </Button>
              </div>

              {panicResult ? (
                <div
                  data-testid="panic-stop-result"
                  className="flex flex-col gap-2 rounded-lg border border-white/10 bg-white/5 p-4 text-sm"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-slate-400">停止後のスケジューラ:</span>
                    <Badge
                      variant={
                        panicResult.scheduler_enabled ? "active" : "muted"
                      }
                    >
                      {panicResult.scheduler_enabled ? "有効" : "無効"}
                    </Badge>
                  </div>
                  <p className="text-slate-300">
                    private 化した本数:{" "}
                    <span className="font-mono">
                      {panicResult.updated_count}
                    </span>
                  </p>
                  <p className="text-slate-300">
                    対象期間内の動画:{" "}
                    <span className="font-mono">
                      {panicResult.recent_videos.length}
                    </span>{" "}
                    件
                  </p>
                  {panicResult.recent_videos.length > 0 ? (
                    <ul className="flex flex-col gap-1 pt-1">
                      {panicResult.recent_videos.map((video) => (
                        <li
                          key={video.youtube_video_id}
                          className="flex items-center justify-between gap-2"
                        >
                          <span
                            className="truncate text-slate-300"
                            title={video.title}
                          >
                            {video.title}
                          </span>
                          <Badge
                            variant={
                              video.privacy_status === "private" ||
                              video.privacy_status === "deleted"
                                ? "muted"
                                : "active"
                            }
                          >
                            {PRIVACY_LABELS[video.privacy_status]}
                          </Badge>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ) : null}
            </CardContent>
          </Card>
        </>
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <div data-testid="panic-stop-dialog" className="contents">
          <DialogHeader>
            <DialogTitle>緊急停止の確認</DialogTitle>
            <DialogDescription>
              スケジューラを停止し、 指定時間内に投稿した動画を private 化します。
              この操作は監査ログに記録されます。
            </DialogDescription>
          </DialogHeader>
          <DialogContent>
            <label
              htmlFor="panic-stop-window"
              className="text-sm text-slate-300"
            >
              対象とする直近の時間(時間)
            </label>
            <Input
              id="panic-stop-window"
              data-testid="panic-stop-window-input"
              type="number"
              min={MIN_WINDOW_HOURS}
              value={windowHoursInput}
              onChange={(event) => setWindowHoursInput(event.target.value)}
            />
            {!windowHoursValid ? (
              <p className="text-xs text-danger">
                {MIN_WINDOW_HOURS} 以上の整数を入力してください。
              </p>
            ) : null}
            {panicMutation.isError ? (
              <p className="text-xs text-danger">
                緊急停止に失敗しました。 時間をおいて再試行してください。
              </p>
            ) : null}
          </DialogContent>
          <Separator />
          <DialogFooter>
            <Button
              data-testid="panic-stop-cancel-btn"
              variant="outline"
              disabled={panicMutation.isPending}
              onClick={() => setDialogOpen(false)}
            >
              キャンセル
            </Button>
            <Button
              data-testid="panic-stop-confirm-btn"
              variant="danger"
              disabled={!windowHoursValid || panicMutation.isPending}
              onClick={() => panicMutation.mutate(parsedWindowHours)}
            >
              緊急停止を実行
            </Button>
          </DialogFooter>
        </div>
      </Dialog>
    </div>
  );
}
