"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BrainCircuit } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { ApiError } from "@/lib/api/client";
import {
  getProviders,
  getUsage,
  setProvider,
  type LlmAuthMode,
  type LlmProvider,
  type LlmProviderConfig,
  type LlmUsage,
} from "@/lib/api/llm";

// US5 T118: マスター LLM provider 切替 + 月次コスト/予算進捗画面(ADR-0019 / ADR-0024)。
// provider/auth_mode/model を select で切替し、 保存(PUT /llm/providers)。 不正組合せ
// (anthropic + codex_oauth など FR-022)は 400 → エラー表示。 model は app_state 非保存
// =参考表示(read-only)。 月次コストは予算進捗バー(50/80/100% で色変化)+ provider 別表。
// backend 未接続時はクエリ失敗を握り潰さず「未接続」として明示する(他画面と同方針)。

const PROVIDERS_QUERY_KEY = ["llm", "providers"] as const;

const PROVIDER_LABELS: Readonly<Record<LlmProvider, string>> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  ollama: "Ollama",
};

const AUTH_MODE_LABELS: Readonly<Record<LlmAuthMode, string>> = {
  api_key: "API キー",
  codex_oauth: "Codex OAuth",
};

const NUMBER_FORMAT = new Intl.NumberFormat("ja-JP");

/** USD コストを `$0.0000` 風(小数 4 桁)で整形する。 */
function formatUsd(value: number): string {
  return `$${value.toFixed(4)}`;
}

/** トークン数を千区切りで整形する。 */
function formatTokens(value: number): string {
  return NUMBER_FORMAT.format(value);
}

/** budget_pct を 0–100 にクランプした表示用パーセント。 */
function clampPct(pct: number): number {
  if (!Number.isFinite(pct) || pct < 0) {
    return 0;
  }
  return Math.min(pct, 100);
}

/** 予算進捗バーの色(tailwind トークン)。 50% 未満=primary、 80% 未満=黄、 以上=danger。 */
function budgetBarColor(pct: number): string {
  if (pct >= 80) {
    return "bg-danger";
  }
  if (pct >= 50) {
    return "bg-amber-400";
  }
  return "bg-primary";
}

/** 直近 12 か月の `YYYY-MM` を新しい順で返す(集計月セレクタ用、 UTC 基準)。 */
function recentMonths(now: Date, count: number): string[] {
  const months: string[] = [];
  const base = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1),
  );
  for (let i = 0; i < count; i += 1) {
    const d = new Date(Date.UTC(base.getUTCFullYear(), base.getUTCMonth() - i, 1));
    const yyyy = d.getUTCFullYear();
    const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
    months.push(`${yyyy}-${mm}`);
  }
  return months;
}

export default function LlmPage(): React.JSX.Element {
  const queryClient = useQueryClient();

  const monthOptions = React.useMemo(() => recentMonths(new Date(), 12), []);
  const [selectedMonth, setSelectedMonth] = React.useState<string>(
    monthOptions[0] ?? "",
  );

  // 選択中の provider/auth_mode。 GET 応答到着後に最初の available を初期選択する。
  const [provider, setSelectedProvider] = React.useState<LlmProvider | null>(
    null,
  );
  const [authMode, setSelectedAuthMode] = React.useState<LlmAuthMode | null>(
    null,
  );

  const providersQuery = useQuery<LlmProviderConfig[]>({
    queryKey: PROVIDERS_QUERY_KEY,
    queryFn: getProviders,
  });

  const usageQuery = useQuery<LlmUsage>({
    queryKey: ["llm", "usage", selectedMonth],
    queryFn: () => getUsage(selectedMonth || undefined),
  });

  const providers = React.useMemo(
    () => providersQuery.data ?? [],
    [providersQuery.data],
  );

  // GET 到着後、 未選択なら先頭 provider を既定選択する(現在 active は contract に無い)。
  React.useEffect(() => {
    if (provider === null && providers.length > 0) {
      const first = providers[0];
      setSelectedProvider(first.provider);
      setSelectedAuthMode(first.auth_modes[0] ?? "api_key");
    }
  }, [provider, providers]);

  const selectedConfig = React.useMemo(
    () => providers.find((p) => p.provider === provider) ?? null,
    [providers, provider],
  );

  // provider 切替時、 その provider が対応しない auth_mode を選んでいたら先頭へ補正する。
  React.useEffect(() => {
    if (!selectedConfig || authMode === null) {
      return;
    }
    if (!selectedConfig.auth_modes.includes(authMode)) {
      setSelectedAuthMode(selectedConfig.auth_modes[0] ?? "api_key");
    }
  }, [selectedConfig, authMode]);

  const invalidateProviders = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: PROVIDERS_QUERY_KEY });

  const saveMutation = useMutation<
    void,
    Error,
    { provider: LlmProvider; authMode: LlmAuthMode }
  >({
    mutationFn: ({ provider: p, authMode: a }) => setProvider(p, a),
    onSuccess: () => {
      void invalidateProviders();
    },
  });

  const handleProviderChange = React.useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>): void => {
      setSelectedProvider(event.target.value as LlmProvider);
    },
    [],
  );

  const handleAuthModeChange = React.useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>): void => {
      setSelectedAuthMode(event.target.value as LlmAuthMode);
    },
    [],
  );

  const handleSave = React.useCallback((): void => {
    if (provider === null || authMode === null) {
      return;
    }
    saveMutation.mutate({ provider, authMode });
  }, [provider, authMode, saveMutation]);

  const saveError = saveMutation.error;
  const saveErrorMessage =
    saveError instanceof ApiError && saveError.status === 400
      ? saveError.message ||
        "選択した認証方式はこの provider では使用できません。"
      : saveError
        ? "provider の保存に失敗しました。 時間をおいて再試行してください。"
        : null;

  const usage = usageQuery.data;
  const budgetPct = usage ? clampPct(usage.budget_pct) : 0;
  const byProviderRows = usage
    ? Object.entries(usage.by_provider).sort((a, b) => b[1].cost_usd - a[1].cost_usd)
    : [];

  const availableModels = selectedConfig?.models ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <BrainCircuit className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">LLM 設定</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>プロバイダ切替</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {providersQuery.isLoading && (
            <p className="text-sm text-slate-500">読み込み中…</p>
          )}

          {providersQuery.isError && (
            <p className="text-sm text-slate-500">
              backend に接続できません(未接続)。
            </p>
          )}

          {!providersQuery.isLoading &&
            !providersQuery.isError &&
            providers.length === 0 && (
              <p className="text-sm text-slate-500">
                利用可能な provider がありません。
              </p>
            )}

          {!providersQuery.isLoading &&
            !providersQuery.isError &&
            providers.length > 0 && (
              <>
                <div className="grid gap-4 sm:grid-cols-3">
                  <div className="flex flex-col gap-1.5">
                    <label
                      htmlFor="llm-provider-select"
                      className="text-xs font-medium text-slate-400"
                    >
                      プロバイダ
                    </label>
                    <select
                      id="llm-provider-select"
                      data-testid="llm-provider-select"
                      value={provider ?? ""}
                      onChange={handleProviderChange}
                      disabled={saveMutation.isPending}
                      className={SELECT_CLASS}
                    >
                      {providers.map((cfg) => (
                        <option key={cfg.provider} value={cfg.provider}>
                          {PROVIDER_LABELS[cfg.provider]}
                          {cfg.available ? "" : "(未設定)"}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label
                      htmlFor="llm-authmode-select"
                      className="text-xs font-medium text-slate-400"
                    >
                      認証方式
                    </label>
                    <select
                      id="llm-authmode-select"
                      data-testid="llm-authmode-select"
                      value={authMode ?? ""}
                      onChange={handleAuthModeChange}
                      disabled={saveMutation.isPending || !selectedConfig}
                      className={SELECT_CLASS}
                    >
                      {(selectedConfig?.auth_modes ?? []).map((mode) => (
                        <option key={mode} value={mode}>
                          {AUTH_MODE_LABELS[mode]}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label
                      htmlFor="llm-model-select"
                      className="text-xs font-medium text-slate-400"
                    >
                      モデル(既定固定)
                    </label>
                    <select
                      id="llm-model-select"
                      data-testid="llm-model-select"
                      value={availableModels[0] ?? ""}
                      disabled
                      className={SELECT_CLASS}
                    >
                      {availableModels.length > 0 ? (
                        availableModels.map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))
                      ) : (
                        <option value="">—</option>
                      )}
                    </select>
                  </div>
                </div>

                {selectedConfig && !selectedConfig.available ? (
                  <p className="text-xs text-amber-400">
                    この provider は API キー/接続が未設定です。 保存しても呼び出しは失敗します。
                  </p>
                ) : null}

                <div className="flex items-center gap-3">
                  <Button
                    data-testid="llm-save-btn"
                    disabled={
                      saveMutation.isPending ||
                      provider === null ||
                      authMode === null
                    }
                    onClick={handleSave}
                  >
                    {saveMutation.isPending ? "保存中…" : "保存"}
                  </Button>
                  {saveMutation.isSuccess && !saveMutation.isPending ? (
                    <span className="text-xs text-primary">保存しました。</span>
                  ) : null}
                </div>

                {saveErrorMessage ? (
                  <p
                    data-testid="llm-save-error"
                    className="text-sm text-danger"
                  >
                    {saveErrorMessage}
                  </p>
                ) : null}
              </>
            )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between gap-4">
            <span>月次コスト / 予算</span>
            <select
              data-testid="llm-usage-month-select"
              value={selectedMonth}
              onChange={(event) => setSelectedMonth(event.target.value)}
              className={cn(SELECT_CLASS, "h-8 w-36 text-xs")}
            >
              {monthOptions.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {usageQuery.isLoading && (
            <p className="text-sm text-slate-500">読み込み中…</p>
          )}

          {usageQuery.isError && (
            <p className="text-sm text-slate-500">
              backend に接続できません(未接続)。
            </p>
          )}

          {!usageQuery.isLoading && !usageQuery.isError && usage && (
            <>
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between gap-4 text-sm">
                  <span className="text-slate-300">
                    {usage.month} の利用額{" "}
                    <span className="font-mono text-slate-100">
                      {formatUsd(usage.total_cost_usd)}
                    </span>{" "}
                    / 予算{" "}
                    <span className="font-mono text-slate-100">
                      {formatUsd(usage.budget_usd)}
                    </span>
                  </span>
                  <span
                    data-testid="llm-budget-pct"
                    className="font-mono text-sm text-slate-200"
                  >
                    {usage.budget_pct.toFixed(1)}%
                  </span>
                </div>
                <div
                  data-testid="llm-budget-bar"
                  className="h-3 w-full overflow-hidden rounded-full bg-white/10"
                  role="progressbar"
                  aria-valuenow={Math.round(budgetPct)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                >
                  <div
                    className={cn(
                      "h-full rounded-full transition-all",
                      budgetBarColor(budgetPct),
                    )}
                    style={{ width: `${budgetPct}%` }}
                  />
                </div>
              </div>

              <Separator />

              {byProviderRows.length === 0 ? (
                <p className="text-sm text-slate-500">
                  この月の利用記録はありません。
                </p>
              ) : (
                <Table data-testid="llm-usage-table">
                  <TableHeader>
                    <TableRow>
                      <TableHead>プロバイダ</TableHead>
                      <TableHead className="text-right">コスト</TableHead>
                      <TableHead className="text-right">入力</TableHead>
                      <TableHead className="text-right">キャッシュ</TableHead>
                      <TableHead className="text-right">出力</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {byProviderRows.map(([name, row]) => (
                      <TableRow key={name}>
                        <TableCell className="font-medium">
                          <Badge variant="active">{name}</Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono text-xs">
                          {formatUsd(row.cost_usd)}
                        </TableCell>
                        <TableCell className="text-right font-mono text-xs">
                          {formatTokens(row.prompt_tokens)}
                        </TableCell>
                        <TableCell className="text-right font-mono text-xs">
                          {formatTokens(row.cached_tokens)}
                        </TableCell>
                        <TableCell className="text-right font-mono text-xs">
                          {formatTokens(row.completion_tokens)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// select は専用 shadcn コンポーネントが無いため Input と同トークンの素の <select>。
const SELECT_CLASS =
  "flex h-9 w-full rounded-lg border border-white/15 bg-white/5 px-3 py-1 text-sm text-slate-100 shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:cursor-not-allowed disabled:opacity-50";
