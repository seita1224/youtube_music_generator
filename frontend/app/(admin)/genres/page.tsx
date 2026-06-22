"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Music2 } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError } from "@/lib/api/client";
import {
  disableGenre,
  listGenres,
  promoteGenre,
  type Genre,
  type GenreListResponse,
} from "@/lib/api/genres";

// 画面10: ジャンル管理(screen-spec.md / US3 / FR-037・FR-038)。
// ジャンル辞書を role 別に一覧し、 experiment ジャンルの採用(promote)/ 削除(disable)を
// 管理 UI から承認する(FR-038: role 遷移 + audit)。 採用/削除「推奨」の判定表示は分析画面
// (/analytics)にあり、 本画面はその最終承認操作を担う。
// backend 未接続時はクエリ失敗を握り潰さず「未接続」として明示する(他画面と同方針)。

const GENRES_QUERY_KEY = ["genres"] as const;

// role(primary/extended/experimental)→ 表示ラベル + バッジ variant。
const ROLE_LABELS: Readonly<Record<string, string>> = {
  primary: "主力",
  extended: "拡張",
  experimental: "実験",
};

const ROLE_BADGE: Readonly<Record<string, "active" | "default" | "muted">> = {
  primary: "active",
  extended: "default",
  experimental: "muted",
};

// 昇格ラダー(experimental → extended → primary)。 primary は最上位で昇格不可。
const ROLE_RANK: Readonly<Record<string, number>> = {
  experimental: 0,
  extended: 1,
  primary: 2,
};

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

function roleRank(role: string): number {
  return ROLE_RANK[role] ?? 0;
}

/** BPM 範囲を `90–110` 風に整形する。 未設定は「—」。 */
function formatBpm(min?: number | null, max?: number | null): string {
  if (min == null && max == null) {
    return "—";
  }
  if (min != null && max != null) {
    return `${min}–${max}`;
  }
  return String(min ?? max);
}

export default function GenresPage(): React.JSX.Element {
  const queryClient = useQueryClient();

  const query = useQuery<GenreListResponse>({
    queryKey: GENRES_QUERY_KEY,
    queryFn: () => listGenres(),
  });

  const invalidate = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: GENRES_QUERY_KEY });

  const promoteMutation = useMutation<Genre, Error, string>({
    mutationFn: (name) => promoteGenre(name),
    onSuccess: () => {
      void invalidate();
    },
  });

  const disableMutation = useMutation<Genre, Error, string>({
    mutationFn: (name) => disableGenre(name),
    onSuccess: () => {
      void invalidate();
    },
  });

  const mutating = promoteMutation.isPending || disableMutation.isPending;

  // 主力 → 拡張 → 実験 の順、 同 role 内は表示名で並べる(管理しやすい順)。
  const genres = React.useMemo(() => {
    const items = [...(query.data?.items ?? [])];
    items.sort((a, b) => {
      const rank = roleRank(b.role) - roleRank(a.role);
      return rank !== 0 ? rank : a.display_name.localeCompare(b.display_name);
    });
    return items;
  }, [query.data]);

  const actionError = promoteMutation.error ?? disableMutation.error;
  const actionErrorMessage =
    actionError instanceof ApiError && actionError.status === 409
      ? actionError.message ||
        "この role 遷移はできません(主力は既に最上位です)。"
      : actionError
        ? "操作に失敗しました。 時間をおいて再試行してください。"
        : null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <Music2 className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">ジャンル管理</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between gap-4">
            <span>ジャンル辞書</span>
            <span className="text-xs font-normal text-slate-500">
              採用/削除の推奨判定は{" "}
              <a href="/analytics" className="text-primary hover:underline">
                分析
              </a>{" "}
              を参照
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {query.isLoading && (
            <p className="text-sm text-slate-500">読み込み中…</p>
          )}

          {query.isError && (
            <p className="text-sm text-slate-500">
              backend に接続できません(未接続)。
            </p>
          )}

          {!query.isLoading && !query.isError && genres.length === 0 && (
            <p className="text-sm text-slate-500">ジャンルがありません。</p>
          )}

          {!query.isLoading && !query.isError && genres.length > 0 && (
            <Table data-testid="genres-table">
              <TableHeader>
                <TableRow>
                  <TableHead>ジャンル</TableHead>
                  <TableHead>役割</TableHead>
                  <TableHead>状態</TableHead>
                  <TableHead className="text-right">BPM</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {genres.map((genre) => {
                  const isPrimary = roleRank(genre.role) >= ROLE_RANK.primary;
                  return (
                    <TableRow key={genre.name} data-testid={`genre-row-${genre.name}`}>
                      <TableCell>
                        <div className="flex flex-col">
                          <span className="font-medium text-slate-200">
                            {genre.display_name}
                          </span>
                          <span className="font-mono text-xs text-slate-500">
                            {genre.name}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge
                          data-testid={`genre-role-${genre.name}`}
                          variant={ROLE_BADGE[genre.role] ?? "muted"}
                        >
                          {roleLabel(genre.role)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant={genre.enabled ? "active" : "muted"}>
                          {genre.enabled ? "有効" : "無効"}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs text-slate-300">
                        {formatBpm(genre.bpm_min, genre.bpm_max)}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          <Button
                            data-testid={`genre-promote-${genre.name}`}
                            variant="outline"
                            disabled={mutating || isPrimary}
                            title={
                              isPrimary
                                ? "主力は既に最上位です"
                                : "採用方向へ 1 段昇格(experimental→extended→primary)"
                            }
                            onClick={() => promoteMutation.mutate(genre.name)}
                          >
                            昇格
                          </Button>
                          <Button
                            data-testid={`genre-disable-${genre.name}`}
                            variant="danger"
                            disabled={mutating || !genre.enabled}
                            onClick={() => disableMutation.mutate(genre.name)}
                          >
                            無効化
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}

          {actionErrorMessage ? (
            <p data-testid="genres-action-error" className="text-sm text-danger">
              {actionErrorMessage}
            </p>
          ) : null}

          <p className="text-xs text-slate-500">
            「昇格」は採用方向へ 1 段(experimental→extended→primary)、 「無効化」は次回サイクル
            以降このジャンルを停止します。 操作は audit_log に記録されます(FR-038)。
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
