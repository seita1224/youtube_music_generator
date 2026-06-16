"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import {
  getPrompt,
  listPrompts,
  type PromptListResponse,
  type PromptSummary,
  type PromptVersion,
} from "@/lib/api/prompts";

// T135 (FR-036): プロンプトの read-only プレビュー画面。
// area で絞り込み → area/name を選択 → version を切替えて本文をプレビュー。
// 書込/編集は MVP 範囲外(read + preview + version 切替のみ)。
// backend 未接続時はクエリ失敗を握り潰さず「未接続」として明示する(他画面と同方針)。

// data-testid 契約(E2E): prompts-area-select / prompts-version-select / prompts-preview。

const PROMPTS_QUERY_KEY = ["prompts"] as const;

/** PromptSummary を安定キー("<area>/<name>")へ変換する。 */
function promptKey(summary: Pick<PromptSummary, "area" | "name">): string {
  return `${summary.area}/${summary.name}`;
}

export default function PromptsPage(): React.JSX.Element {
  // area 絞り込み("" = 全 area)。 area/name 選択("<area>/<name>")。 version 選択(番号)。
  const [selectedArea, setSelectedArea] = React.useState<string>("");
  const [selectedKey, setSelectedKey] = React.useState<string>("");
  const [selectedVersion, setSelectedVersion] = React.useState<number | null>(
    null,
  );

  const listQuery = useQuery<PromptListResponse>({
    queryKey: [...PROMPTS_QUERY_KEY, "list", selectedArea],
    queryFn: () => listPrompts(selectedArea || undefined),
  });

  const items = React.useMemo(
    () => listQuery.data?.items ?? [],
    [listQuery.data],
  );

  // area 絞り込み select の選択肢は「全 prompt の area 集合」から作るため、 area 無指定で
  // 取得した一覧を保持する。 area を絞ると items から area が 1 種になり選択肢が痩せるのを防ぐ。
  const allAreasQuery = useQuery<PromptListResponse>({
    queryKey: [...PROMPTS_QUERY_KEY, "areas"],
    queryFn: () => listPrompts(),
  });

  const areaOptions = React.useMemo(() => {
    const areas = new Set<string>();
    for (const item of allAreasQuery.data?.items ?? []) {
      areas.add(item.area);
    }
    return Array.from(areas).sort();
  }, [allAreasQuery.data]);

  // 一覧到着後、 未選択 or 現選択が一覧外なら先頭 prompt を既定選択する。
  React.useEffect(() => {
    if (items.length === 0) {
      return;
    }
    const exists = items.some((item) => promptKey(item) === selectedKey);
    if (selectedKey === "" || !exists) {
      setSelectedKey(promptKey(items[0]));
    }
  }, [items, selectedKey]);

  const selectedSummary = React.useMemo(
    () => items.find((item) => promptKey(item) === selectedKey) ?? null,
    [items, selectedKey],
  );

  // prompt 切替時、 version 未選択 or その prompt に無い version を選んでいたら最新へ補正する。
  React.useEffect(() => {
    if (!selectedSummary || selectedSummary.versions.length === 0) {
      setSelectedVersion(null);
      return;
    }
    const latest =
      selectedSummary.latest ??
      selectedSummary.versions[selectedSummary.versions.length - 1];
    if (
      selectedVersion === null ||
      !selectedSummary.versions.includes(selectedVersion)
    ) {
      setSelectedVersion(latest);
    }
  }, [selectedSummary, selectedVersion]);

  const previewQuery = useQuery<PromptVersion>({
    queryKey: [...PROMPTS_QUERY_KEY, "preview", selectedKey, selectedVersion],
    queryFn: () => getPrompt(selectedKey, selectedVersion ?? undefined),
    enabled: selectedKey !== "" && selectedVersion !== null,
  });

  const handleAreaChange = React.useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>): void => {
      setSelectedArea(event.target.value);
      // area を変えると prompt 集合が変わるため選択をリセットし、 一覧到着後に先頭を選ばせる。
      setSelectedKey("");
      setSelectedVersion(null);
    },
    [],
  );

  const handleKeyChange = React.useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>): void => {
      setSelectedKey(event.target.value);
      setSelectedVersion(null);
    },
    [],
  );

  const handleVersionChange = React.useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>): void => {
      const next = Number.parseInt(event.target.value, 10);
      setSelectedVersion(Number.isNaN(next) ? null : next);
    },
    [],
  );

  const versions = selectedSummary?.versions ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <FileText className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">プロンプト</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>プロンプト選択</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {listQuery.isLoading && (
            <p className="text-sm text-slate-500">読み込み中…</p>
          )}

          {listQuery.isError && (
            <p className="text-sm text-slate-500">
              backend に接続できません(未接続)。
            </p>
          )}

          {!listQuery.isLoading && !listQuery.isError && items.length === 0 && (
            <p className="text-sm text-slate-500">
              利用可能なプロンプトがありません。
            </p>
          )}

          {!listQuery.isLoading && !listQuery.isError && items.length > 0 && (
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="prompts-area-select"
                  className="text-xs font-medium text-slate-400"
                >
                  エリア
                </label>
                <select
                  id="prompts-area-select"
                  data-testid="prompts-area-select"
                  value={selectedArea}
                  onChange={handleAreaChange}
                  className={SELECT_CLASS}
                >
                  <option value="">すべて</option>
                  {areaOptions.map((area) => (
                    <option key={area} value={area}>
                      {area}
                    </option>
                  ))}
                </select>
              </div>

              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="prompts-name-select"
                  className="text-xs font-medium text-slate-400"
                >
                  プロンプト
                </label>
                <select
                  id="prompts-name-select"
                  data-testid="prompts-name-select"
                  value={selectedKey}
                  onChange={handleKeyChange}
                  className={SELECT_CLASS}
                >
                  {items.map((item) => {
                    const key = promptKey(item);
                    return (
                      <option key={key} value={key}>
                        {key}
                      </option>
                    );
                  })}
                </select>
              </div>

              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="prompts-version-select"
                  className="text-xs font-medium text-slate-400"
                >
                  バージョン
                </label>
                <select
                  id="prompts-version-select"
                  data-testid="prompts-version-select"
                  value={selectedVersion ?? ""}
                  onChange={handleVersionChange}
                  disabled={versions.length === 0}
                  className={SELECT_CLASS}
                >
                  {versions.length > 0 ? (
                    versions.map((v) => (
                      <option key={v} value={v}>
                        v{v}
                        {selectedSummary?.latest === v ? "(最新)" : ""}
                      </option>
                    ))
                  ) : (
                    <option value="">—</option>
                  )}
                </select>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between gap-4">
            <span>プレビュー</span>
            {previewQuery.data ? (
              <span className="font-mono text-xs text-slate-400">
                {previewQuery.data.name}
              </span>
            ) : null}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {previewQuery.isLoading && (
            <p className="text-sm text-slate-500">読み込み中…</p>
          )}

          {previewQuery.isError && (
            <p className="text-sm text-slate-500">
              backend に接続できません(未接続)。
            </p>
          )}

          {!previewQuery.isLoading &&
            !previewQuery.isError &&
            !previewQuery.data && (
              <p className="text-sm text-slate-500">
                プロンプトを選択してください。
              </p>
            )}

          {!previewQuery.isLoading &&
            !previewQuery.isError &&
            previewQuery.data && (
              <pre
                data-testid="prompts-preview"
                className={cn(
                  "max-h-[32rem] overflow-auto rounded-lg border border-white/10 bg-black/30 p-4",
                  "whitespace-pre-wrap break-words font-mono text-xs text-slate-200",
                )}
              >
                {previewQuery.data.content}
              </pre>
            )}
        </CardContent>
      </Card>
    </div>
  );
}

// select は専用 shadcn コンポーネントが無いため Input と同トークンの素の <select>(llm/page.tsx と同一)。
const SELECT_CLASS =
  "flex h-9 w-full rounded-lg border border-white/15 bg-white/5 px-3 py-1 text-sm text-slate-100 shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:cursor-not-allowed disabled:opacity-50";
