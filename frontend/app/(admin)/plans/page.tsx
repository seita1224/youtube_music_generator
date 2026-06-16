"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  approvePlan,
  listPlans,
  type Plan,
  type PlanCycle,
  type PlanListResponse,
  type PlanStatus,
} from "@/lib/api/plans";

// US3: 改善プラン一覧画面。 cycle タブ(daily/weekly)でフィルタし、 各プランを
// card で並べる。 payload の rationale / genre_distribution 等の要点を抜き出して表示し、
// status=generated のプランは承認ボタン(useMutation→invalidate)で承認できる。
// backend 未接続時はクエリ失敗を握り潰さず「未接続」として明示する(dryrun 画面踏襲)。

interface CycleTab {
  readonly value: PlanCycle;
  readonly label: string;
}

// タブ並びは運用順(週次の改善計画 → 日次の実行計画)。
const CYCLE_TABS: readonly CycleTab[] = [
  { value: "weekly", label: "週次" },
  { value: "daily", label: "日次" },
];

const CYCLE_LABELS: Readonly<Record<PlanCycle, string>> = {
  daily: "日次",
  weekly: "週次",
};

const STATUS_LABELS: Readonly<Record<PlanStatus, string>> = {
  generated: "生成済",
  approved: "承認済",
  executing: "実行中",
  completed: "完了",
  failed: "失敗",
};

// status → Badge variant。 generated=要対応(active)、 実行中/完了=default、
// 承認済=muted(対応不要)、 失敗=danger。
const STATUS_BADGE_VARIANT: Readonly<
  Record<PlanStatus, "default" | "active" | "danger" | "muted">
> = {
  generated: "active",
  approved: "muted",
  executing: "default",
  completed: "default",
  failed: "danger",
};

/** ISO date 文字列を `YYYY-MM-DD` 風に整形。 未指定/不正値はそのまま「—」。 */
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

/** payload から genre_distribution(`{genre: ratio}`)を安全に取り出す。 不正形は空配列。 */
function readGenreDistribution(
  payload: Record<string, unknown>,
): readonly (readonly [string, number])[] {
  const raw = payload["genre_distribution"];
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) {
    return [];
  }
  return Object.entries(raw as Record<string, unknown>)
    .filter((entry): entry is [string, number] => typeof entry[1] === "number")
    .sort((a, b) => b[1] - a[1]);
}

/** payload から文字列配列(avoid_genres 等)を安全に取り出す。 */
function readStringArray(
  payload: Record<string, unknown>,
  key: string,
): readonly string[] {
  const raw = payload[key];
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.filter((item): item is string => typeof item === "string");
}

export default function PlansListPage(): React.JSX.Element {
  const [cycle, setCycle] = React.useState<PlanCycle>("weekly");
  const queryClient = useQueryClient();

  const query = useQuery<PlanListResponse>({
    queryKey: ["plans", cycle],
    queryFn: () => listPlans(cycle),
  });

  const approveMutation = useMutation({
    mutationFn: (id: string) => approvePlan(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["plans"] });
    },
  });

  const handleCycleChange = React.useCallback((value: string): void => {
    setCycle(value as PlanCycle);
  }, []);

  const items = query.data?.items ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <CalendarClock className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">改善プラン</h1>
      </div>

      <Tabs
        data-testid="plan-cycle-filter"
        value={cycle}
        onValueChange={handleCycleChange}
      >
        <TabsList>
          {CYCLE_TABS.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {query.isLoading && <p className="text-sm text-slate-500">読み込み中…</p>}

      {query.isError && (
        <p className="text-sm text-slate-500">
          backend に接続できません(未接続)。
        </p>
      )}

      {!query.isLoading && !query.isError && items.length === 0 && (
        <p data-testid="plan-list-empty" className="text-sm text-slate-500">
          {CYCLE_LABELS[cycle]}のプランはまだありません。
        </p>
      )}

      {!query.isLoading && !query.isError && items.length > 0 && (
        <div className="flex flex-col gap-4">
          {items.map((plan) => (
            <PlanCard
              key={plan.id}
              plan={plan}
              onApprove={() => approveMutation.mutate(plan.id)}
              approving={
                approveMutation.isPending &&
                approveMutation.variables === plan.id
              }
              approveFailed={
                approveMutation.isError &&
                approveMutation.variables === plan.id
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface PlanCardProps {
  readonly plan: Plan;
  readonly onApprove: () => void;
  readonly approving: boolean;
  readonly approveFailed: boolean;
}

function PlanCard({
  plan,
  onApprove,
  approving,
  approveFailed,
}: PlanCardProps): React.JSX.Element {
  const genreDistribution = readGenreDistribution(plan.payload);
  const avoidGenres = readStringArray(plan.payload, "avoid_genres");
  // weekly は target_week_start、 daily は target_date を主たる対象期間として表示。
  const targetLabel =
    plan.cycle === "weekly"
      ? `週開始 ${formatDate(plan.target_week_start)}`
      : `対象日 ${formatDate(plan.target_date)}`;
  const canApprove = plan.status === "generated";

  return (
    <Card data-testid="plan-card">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-col gap-1">
            <CardTitle className="text-slate-300">{targetLabel}</CardTitle>
            <p className="font-mono text-xs text-slate-500" title={plan.id}>
              {plan.id}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="default">{CYCLE_LABELS[plan.cycle]}</Badge>
            <Badge
              data-testid="plan-status-badge"
              variant={STATUS_BADGE_VARIANT[plan.status]}
            >
              {STATUS_LABELS[plan.status]}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="whitespace-pre-wrap text-sm text-slate-200">
          {plan.rationale}
        </p>

        {genreDistribution.length > 0 && (
          <div className="flex flex-col gap-2">
            <p className="text-xs font-medium text-slate-400">ジャンル配分</p>
            <div className="flex flex-wrap gap-2">
              {genreDistribution.map(([genre, ratio]) => (
                <Badge key={genre} variant="active">
                  {genre} {Math.round(ratio * 100)}%
                </Badge>
              ))}
            </div>
          </div>
        )}

        {avoidGenres.length > 0 && (
          <div className="flex flex-col gap-2">
            <p className="text-xs font-medium text-slate-400">回避ジャンル</p>
            <div className="flex flex-wrap gap-2">
              {avoidGenres.map((genre) => (
                <Badge key={genre} variant="danger">
                  {genre}
                </Badge>
              ))}
            </div>
          </div>
        )}

        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
          <MetaRow label="LLM" value={`${plan.llm_provider} / ${plan.llm_model}`} />
          <MetaRow label="プロンプト版" value={plan.llm_prompt_version} />
          <MetaRow label="コスト(USD)" value={`$${plan.llm_cost_usd}`} />
          <MetaRow label="生成日時" value={formatDate(plan.created_at)} />
          <MetaRow label="承認日時" value={formatDate(plan.approved_at)} />
        </dl>

        {canApprove && (
          <>
            <Separator />
            <div className="flex flex-col gap-2">
              <div className="flex justify-end">
                <Button
                  data-testid="plan-approve-btn"
                  variant="default"
                  size="sm"
                  disabled={approving}
                  onClick={onApprove}
                >
                  承認する
                </Button>
              </div>
              {approveFailed && (
                <p className="text-right text-xs text-danger">
                  承認に失敗しました。 時間をおいて再試行してください。
                </p>
              )}
            </div>
          </>
        )}
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
      <dt className="text-slate-500">{label}</dt>
      <dd className="break-all text-slate-300">{value}</dd>
    </div>
  );
}
