"use client";

import { useQuery } from "@tanstack/react-query";
import {
  CalendarClock,
  Activity,
  BrainCircuit,
  CheckCircle2,
  HeartPulse,
} from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getHealth, type HealthResponse } from "@/lib/api/health";
import { getSchedulerState, type SchedulerState } from "@/lib/api/scheduler";

// T067 + T068: ダッシュボードホーム(screen-spec.md §2 ①)。
// scheduler 状態 + 直近 jobs 件数 + LLM provider 表示 + MVP x/6 小インジケータ + /health 表示。
// backend 未接続時はクエリ失敗を握り潰さず「未接続」として明示する(推測で埋めない)。

const MVP_TOTAL = 6;

function StatusValue({
  loading,
  error,
  children,
}: {
  readonly loading: boolean;
  readonly error: boolean;
  readonly children: React.ReactNode;
}): React.JSX.Element {
  if (loading) {
    return <span className="text-slate-500">読み込み中…</span>;
  }
  if (error) {
    return <span className="text-slate-500">未接続</span>;
  }
  return <>{children}</>;
}

export default function DashboardPage(): React.JSX.Element {
  const health = useQuery<HealthResponse>({
    queryKey: ["health"],
    queryFn: getHealth,
  });
  const scheduler = useQuery<SchedulerState>({
    queryKey: ["scheduler"],
    queryFn: getSchedulerState,
  });

  const schedulerEnabled =
    scheduler.data?.enabled ?? health.data?.scheduler_enabled ?? false;
  const llmProvider = health.data?.llm_provider ?? "—";

  return (
    <div className="flex flex-col gap-6">
      {/* 上段 KPI: scheduler / 直近 jobs / LLM provider */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <CalendarClock className="h-4 w-4" aria-hidden="true" />
              スケジューラ状態
            </CardTitle>
          </CardHeader>
          <CardContent>
            <StatusValue
              loading={scheduler.isLoading && health.isLoading}
              error={scheduler.isError && health.isError}
            >
              <Badge variant={schedulerEnabled ? "active" : "muted"}>
                {schedulerEnabled ? "稼働中" : "停止中"}
              </Badge>
            </StatusValue>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Activity className="h-4 w-4" aria-hidden="true" />
              直近のジョブ
            </CardTitle>
          </CardHeader>
          <CardContent>
            {/* /jobs はリアルタイム SSE(contracts: /jobs/stream)。 件数 API は未契約のため
                ジョブ進捗画面への導線を提示し、 数値の捏造はしない。 */}
            <a href="/jobs" className="text-sm text-primary hover:underline">
              ジョブ進捗を開く →
            </a>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <BrainCircuit className="h-4 w-4" aria-hidden="true" />
              LLM プロバイダ
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center justify-between">
              <StatusValue loading={health.isLoading} error={health.isError}>
                <span className="font-mono text-sm text-slate-200">{llmProvider}</span>
              </StatusValue>
              <a href="/llm" className="text-xs text-primary hover:underline">
                切替
              </a>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* /health パネル(T068) */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <HeartPulse className="h-4 w-4" aria-hidden="true" />
            backend ヘルスチェック(/health)
          </CardTitle>
        </CardHeader>
        <CardContent>
          {health.isLoading && (
            <p className="text-sm text-slate-500">読み込み中…</p>
          )}
          {health.isError && (
            <p className="text-sm text-slate-500">
              backend に接続できません(未接続)。
            </p>
          )}
          {health.data && (
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
              <HealthRow label="status" value={health.data.status} />
              <HealthRow label="db" value={health.data.db ?? "—"} />
              <HealthRow label="gpu_worker" value={health.data.gpu_worker ?? "—"} />
              <HealthRow
                label="scheduler"
                value={health.data.scheduler_enabled ? "有効" : "無効"}
              />
              <HealthRow
                label="dryrun"
                value={health.data.dryrun_enabled ? "有効" : "無効"}
              />
              <HealthRow label="llm_provider" value={health.data.llm_provider ?? "—"} />
            </dl>
          )}
        </CardContent>
      </Card>

      {/* 下段 MVP 完了状況の小インジケータ(screen-spec.md §0 / §2 ①)。
          x/6。 /mvp-check は仕様外の内部 endpoint のため数値は backend 連携待ち。 */}
      <div className="flex items-center gap-2 text-xs text-slate-500">
        <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
        <span>
          MVP 完了状況: <span className="font-mono">—/{MVP_TOTAL}</span>
          (詳細は内部 /mvp-check)
        </span>
      </div>
    </div>
  );
}

function HealthRow({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}): React.JSX.Element {
  return (
    <div className="flex flex-col">
      <dt className="text-slate-500">{label}</dt>
      <dd className="font-mono text-slate-200">{value}</dd>
    </div>
  );
}
