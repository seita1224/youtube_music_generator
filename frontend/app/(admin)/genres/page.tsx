"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Music2, Plus } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
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
  createGenre,
  disableGenre,
  demoteGenre,
  listGenres,
  promoteGenre,
  type Genre,
  type GenreListResponse,
  type GenreRole,
} from "@/lib/api/genres";

// 画面10: ジャンル管理(screen-spec.md / US3 / FR-037・FR-038)。
// ジャンル辞書を role 別に一覧し、 採用(promote)/ 降格(demote)/ 削除(disable)/ 手動追加
// (create)を管理 UI から操作する(FR-038: role 遷移 + audit)。 採用/削除の「推奨」判定表示は
// 分析画面(/analytics)にあり、 本画面はその最終承認 + 辞書編集を担う。
// role 語彙は spec 準拠の experiment / extension / main。

const GENRES_QUERY_KEY = ["genres"] as const;

const ROLE_LABELS: Readonly<Record<string, string>> = {
  main: "主力",
  extension: "拡張",
  experiment: "実験",
};

const ROLE_BADGE: Readonly<Record<string, "active" | "default" | "muted">> = {
  main: "active",
  extension: "default",
  experiment: "muted",
};

// 昇格ラダー(experiment → extension → main)。 main 最上位、 experiment 最下位。
const ROLE_RANK: Readonly<Record<string, number>> = {
  experiment: 0,
  extension: 1,
  main: 2,
};

const ROLE_OPTIONS: readonly GenreRole[] = ["experiment", "extension", "main"];

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

const SELECT_CLASS =
  "flex h-9 w-full rounded-lg border border-white/15 bg-white/5 px-3 py-1 text-sm text-slate-100 shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:cursor-not-allowed disabled:opacity-50";

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
    onSuccess: () => void invalidate(),
  });

  const demoteMutation = useMutation<Genre, Error, string>({
    mutationFn: (name) => demoteGenre(name),
    onSuccess: () => void invalidate(),
  });

  const disableMutation = useMutation<Genre, Error, string>({
    mutationFn: (name) => disableGenre(name),
    onSuccess: () => void invalidate(),
  });

  const rowMutating =
    promoteMutation.isPending ||
    demoteMutation.isPending ||
    disableMutation.isPending;

  // --- 新規追加フォーム --------------------------------------------------------
  const [name, setName] = React.useState("");
  const [displayName, setDisplayName] = React.useState("");
  const [role, setRole] = React.useState<GenreRole>("experiment");
  const [bpmMin, setBpmMin] = React.useState("");
  const [bpmMax, setBpmMax] = React.useState("");

  const createMutation = useMutation<Genre, Error, void>({
    mutationFn: () =>
      createGenre({
        name: name.trim(),
        display_name: displayName.trim(),
        role,
        bpm_min: bpmMin ? Number.parseInt(bpmMin, 10) : null,
        bpm_max: bpmMax ? Number.parseInt(bpmMax, 10) : null,
      }),
    onSuccess: () => {
      setName("");
      setDisplayName("");
      setRole("experiment");
      setBpmMin("");
      setBpmMax("");
      void invalidate();
    },
  });

  const createValid = name.trim().length > 0 && displayName.trim().length > 0;
  const createError = createMutation.error;
  const createErrorMessage =
    createError instanceof ApiError && createError.status === 409
      ? "同じ名前のジャンルが既に存在します。"
      : createError
        ? "追加に失敗しました。 時間をおいて再試行してください。"
        : null;

  // 主力 → 拡張 → 実験 の順、 同 role 内は表示名で並べる。
  const genres = React.useMemo(() => {
    const items = [...(query.data?.items ?? [])];
    items.sort((a, b) => {
      const rank = roleRank(b.role) - roleRank(a.role);
      return rank !== 0 ? rank : a.display_name.localeCompare(b.display_name);
    });
    return items;
  }, [query.data]);

  const rowError =
    promoteMutation.error ?? demoteMutation.error ?? disableMutation.error;
  const rowErrorMessage =
    rowError instanceof ApiError && rowError.status === 409
      ? rowError.message || "この role 遷移はできません。"
      : rowError
        ? "操作に失敗しました。 時間をおいて再試行してください。"
        : null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <Music2 className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">ジャンル管理</h1>
      </div>

      {/* 新規追加フォーム */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Plus className="h-4 w-4" aria-hidden="true" />
            ジャンルを追加
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="genre-name" className="text-xs text-slate-400">
                name(slug)
              </label>
              <Input
                id="genre-name"
                data-testid="genre-create-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="future-garage"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="genre-display" className="text-xs text-slate-400">
                表示名
              </label>
              <Input
                id="genre-display"
                data-testid="genre-create-display-name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Future Garage"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="genre-role" className="text-xs text-slate-400">
                役割
              </label>
              <select
                id="genre-role"
                data-testid="genre-create-role"
                value={role}
                onChange={(e) => setRole(e.target.value as GenreRole)}
                className={SELECT_CLASS}
              >
                {ROLE_OPTIONS.map((r) => (
                  <option key={r} value={r}>
                    {roleLabel(r)}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="genre-bpm-min" className="text-xs text-slate-400">
                BPM min
              </label>
              <Input
                id="genre-bpm-min"
                data-testid="genre-create-bpm-min"
                type="number"
                value={bpmMin}
                onChange={(e) => setBpmMin(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="genre-bpm-max" className="text-xs text-slate-400">
                BPM max
              </label>
              <Input
                id="genre-bpm-max"
                data-testid="genre-create-bpm-max"
                type="number"
                value={bpmMax}
                onChange={(e) => setBpmMax(e.target.value)}
              />
            </div>
          </div>
          <div className="flex items-center gap-3">
            <Button
              data-testid="genre-create-submit"
              disabled={!createValid || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {createMutation.isPending ? "追加中…" : "追加"}
            </Button>
            {createMutation.isSuccess && !createMutation.isPending ? (
              <span className="text-xs text-primary">追加しました。</span>
            ) : null}
            {createErrorMessage ? (
              <span data-testid="genre-create-error" className="text-sm text-danger">
                {createErrorMessage}
              </span>
            ) : null}
          </div>
          <p className="text-xs text-slate-500">
            新規ジャンルは既定で「実験(experiment)」として追加され、 retention 実績に応じて
            昇格/降格します(spec.md の experiment_slot 運用に整合)。
          </p>
        </CardContent>
      </Card>

      {/* ジャンル一覧 + 操作 */}
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
                  const isTop = roleRank(genre.role) >= ROLE_RANK.main;
                  const isBottom = roleRank(genre.role) <= ROLE_RANK.experiment;
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
                            disabled={rowMutating || isTop}
                            title="採用方向へ 1 段昇格(experiment→extension→main)"
                            onClick={() => promoteMutation.mutate(genre.name)}
                          >
                            昇格
                          </Button>
                          <Button
                            data-testid={`genre-demote-${genre.name}`}
                            variant="outline"
                            disabled={rowMutating || isBottom}
                            title="1 段降格(main→extension→experiment、 enabled は不変)"
                            onClick={() => demoteMutation.mutate(genre.name)}
                          >
                            降格
                          </Button>
                          <Button
                            data-testid={`genre-disable-${genre.name}`}
                            variant="danger"
                            disabled={rowMutating || !genre.enabled}
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

          {rowErrorMessage ? (
            <p data-testid="genres-action-error" className="text-sm text-danger">
              {rowErrorMessage}
            </p>
          ) : null}

          <Separator />
          <p className="text-xs text-slate-500">
            「昇格」採用方向へ 1 段 / 「降格」逆へ 1 段(enabled 不変)/ 「無効化」次回サイクル以降
            停止。 すべて audit_log に記録されます(FR-038)。
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
