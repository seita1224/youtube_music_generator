"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { BarChart3 } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  getAnalytics,
  type AnalyticsOverview,
  type GenreAnalytics,
  type GenreRecommendation,
  type RecommendAction,
} from "@/lib/api/analytics";

// T110 frontend: ジャンル別 retention/views 分析 + 採用/削除推奨画面(US3)。
// window_days をタブで切替し、 ジャンル別 retention(LineChart)/views(BarChart)を
// recharts で描画する。 experiment ジャンルの採用/削除推奨は表で並べる。
// backend 未接続時はクエリ失敗を握り潰さず「未接続」として明示する。

// 集計ウィンドウ候補。 backend 既定は 14 日(contract)。
const WINDOW_OPTIONS: readonly number[] = [7, 14, 30, 90];

// Synthetix Vibe 配色(tailwind トークン)。 primary=#a855f7 / danger=#ef4444。
const COLOR_PRIMARY = "#a855f7";
const COLOR_VIEWS = "#38bdf8"; // sky-400: views 系列を retention と色分け
const COLOR_GRID = "rgba(255,255,255,0.08)";
const COLOR_AXIS = "#94a3b8"; // slate-400

const ACTION_LABELS: Readonly<Record<RecommendAction, string>> = {
  adopt: "採用推奨",
  drop: "削除推奨",
  keep: "継続観察",
};

// adopt=注目(active=紫)、 drop=danger(赤)、 keep=muted。
const ACTION_BADGE_VARIANT: Readonly<
  Record<RecommendAction, "default" | "active" | "danger" | "muted">
> = {
  adopt: "active",
  drop: "danger",
  keep: "muted",
};

/** retention 比を `0.82` → `82%` 風に整形。 null は「—」。 */
function formatRatio(ratio: number | null): string {
  if (ratio === null) {
    return "—";
  }
  return `${(ratio * 100).toFixed(0)}%`;
}

/** view 数を千区切りで整形。 */
function formatViews(views: number): string {
  return new Intl.NumberFormat("ja-JP").format(views);
}

interface ChartDatum {
  readonly genre: string;
  readonly retention: number | null;
  readonly views: number;
}

function toChartData(byGenre: readonly GenreAnalytics[]): ChartDatum[] {
  return byGenre.map((g) => ({
    genre: g.genre,
    retention: g.avg_retention_pct,
    views: g.total_views,
  }));
}

export default function AnalyticsPage(): React.JSX.Element {
  const [windowDays, setWindowDays] = React.useState<number>(14);

  const query = useQuery<AnalyticsOverview>({
    queryKey: ["analytics", windowDays],
    queryFn: () => getAnalytics(windowDays),
  });

  const handleWindowChange = React.useCallback((value: string): void => {
    setWindowDays(Number(value));
  }, []);

  const overview = query.data;
  const recommendations = overview?.recommendations ?? [];
  const chartData = React.useMemo(
    () => toChartData(overview?.by_genre ?? []),
    [overview],
  );
  const hasData = chartData.length > 0;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <BarChart3 className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">ジャンル分析</h1>
      </div>

      <div className="flex items-center justify-between gap-4">
        <Tabs value={String(windowDays)} onValueChange={handleWindowChange}>
          <TabsList data-testid="analytics-window-select">
            {WINDOW_OPTIONS.map((days) => (
              <TabsTrigger key={days} value={String(days)}>
                {days}日
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        {overview ? (
          <p className="text-xs text-slate-500">
            標本数: {overview.sample_size}本 / {overview.window_days}日
          </p>
        ) : null}
      </div>

      {query.isLoading && (
        <p className="text-sm text-slate-500">読み込み中…</p>
      )}

      {query.isError && (
        <p className="text-sm text-slate-500">
          backend に接続できません(未接続)。
        </p>
      )}

      {!query.isLoading && !query.isError && !hasData && (
        <p data-testid="analytics-empty" className="text-sm text-slate-500">
          集計対象のデータがありません。
        </p>
      )}

      {!query.isLoading && !query.isError && hasData && (
        <div data-testid="analytics-chart" className="flex flex-col gap-6">
          <RetentionChart data={chartData} />
          <ViewsChart data={chartData} />
        </div>
      )}

      {!query.isLoading && !query.isError && (
        <RecommendationTable recommendations={recommendations} />
      )}
    </div>
  );
}

function RetentionChart({
  data,
}: {
  readonly data: readonly ChartDatum[];
}): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>ジャンル別 平均視聴維持率(retention %)</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="h-72 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={data as ChartDatum[]}
              margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
            >
              <CartesianGrid stroke={COLOR_GRID} vertical={false} />
              <XAxis
                dataKey="genre"
                tick={{ fill: COLOR_AXIS, fontSize: 12 }}
                stroke={COLOR_GRID}
              />
              <YAxis
                tick={{ fill: COLOR_AXIS, fontSize: 12 }}
                stroke={COLOR_GRID}
                unit="%"
              />
              <Tooltip
                contentStyle={{
                  background: "#0f172a",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: 8,
                  color: "#e2e8f0",
                }}
                formatter={(value: number | string) => [`${value}%`, "retention"]}
              />
              <Legend wrapperStyle={{ color: COLOR_AXIS, fontSize: 12 }} />
              <Line
                type="monotone"
                dataKey="retention"
                name="平均視聴維持率"
                stroke={COLOR_PRIMARY}
                strokeWidth={2}
                dot={{ r: 3, fill: COLOR_PRIMARY }}
                connectNulls
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}

function ViewsChart({
  data,
}: {
  readonly data: readonly ChartDatum[];
}): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>ジャンル別 総再生数(views)</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="h-72 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={data as ChartDatum[]}
              margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
            >
              <CartesianGrid stroke={COLOR_GRID} vertical={false} />
              <XAxis
                dataKey="genre"
                tick={{ fill: COLOR_AXIS, fontSize: 12 }}
                stroke={COLOR_GRID}
              />
              <YAxis
                tick={{ fill: COLOR_AXIS, fontSize: 12 }}
                stroke={COLOR_GRID}
              />
              <Tooltip
                cursor={{ fill: "rgba(255,255,255,0.04)" }}
                contentStyle={{
                  background: "#0f172a",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: 8,
                  color: "#e2e8f0",
                }}
                formatter={(value: number | string) => [
                  formatViews(Number(value)),
                  "views",
                ]}
              />
              <Legend wrapperStyle={{ color: COLOR_AXIS, fontSize: 12 }} />
              <Bar
                dataKey="views"
                name="総再生数"
                fill={COLOR_VIEWS}
                radius={[4, 4, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}

function RecommendationTable({
  recommendations,
}: {
  readonly recommendations: readonly GenreRecommendation[];
}): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>実験ジャンルの採用/削除推奨</CardTitle>
      </CardHeader>
      <CardContent>
        {recommendations.length === 0 ? (
          <p className="text-sm text-slate-500">
            判定対象の実験ジャンルはありません。
          </p>
        ) : (
          <Table data-testid="genre-recommend-table">
            <TableHeader>
              <TableRow>
                <TableHead>ジャンル</TableHead>
                <TableHead>推奨</TableHead>
                <TableHead className="text-right">主力比</TableHead>
                <TableHead className="text-right">本数</TableHead>
                <TableHead className="text-right">経過日数</TableHead>
                <TableHead>判定理由</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {recommendations.map((rec) => (
                <TableRow key={rec.genre} data-testid="genre-recommend-row">
                  <TableCell className="font-medium">{rec.genre}</TableCell>
                  <TableCell>
                    <Badge
                      data-testid="genre-recommend-action"
                      variant={ACTION_BADGE_VARIANT[rec.action]}
                    >
                      {ACTION_LABELS[rec.action]}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {formatRatio(rec.retention_ratio_to_primary)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {rec.sample_size}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {rec.days_elapsed}
                  </TableCell>
                  <TableCell className="text-slate-400">
                    {rec.rationale}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
