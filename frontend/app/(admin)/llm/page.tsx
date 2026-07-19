"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BrainCircuit } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
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
  clearCredential,
  getLlmSettings,
  getUsage,
  LLM_API_KEY_MAX_LENGTH,
  setCredential,
  setProvider,
  type LlmAuthMode,
  type LlmCredentialSource,
  type LlmProvider,
  type LlmProviderConfig,
  type LlmSecretProvider,
  type LlmSettings,
  type LlmUsage,
} from "@/lib/api/llm";

// US5: マスター LLM provider / model / write-only API key 管理 + 月次コスト(ADR-0019 / ADR-0024)。
// GET の active を初期選択に使う。 Codex OAuth は未配線のため選択不可。
// env が SoT のとき API key 入力は非表示。 平文キーは応答に出ない。

const SETTINGS_QUERY_KEY = ["llm", "providers"] as const;

const PROVIDER_LABELS: Readonly<Record<LlmProvider, string>> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  ollama: "Ollama",
};

const AUTH_MODE_LABELS: Readonly<Record<LlmAuthMode, string>> = {
  api_key: "API キー",
  codex_oauth: "Codex OAuth（未対応）",
};

const SOURCE_LABELS: Readonly<Record<LlmCredentialSource, string>> = {
  env: "環境変数",
  db: "DB（暗号化）",
  none: "未設定",
  "n/a": "不要",
};

const NUMBER_FORMAT = new Intl.NumberFormat("ja-JP");

function formatUsd(value: number): string {
  return `$${value.toFixed(4)}`;
}

function formatTokens(value: number): string {
  return NUMBER_FORMAT.format(value);
}

function clampPct(pct: number): number {
  if (!Number.isFinite(pct) || pct < 0) {
    return 0;
  }
  return Math.min(pct, 100);
}

function budgetBarColor(pct: number): string {
  if (pct >= 80) {
    return "bg-danger";
  }
  if (pct >= 50) {
    return "bg-amber-400";
  }
  return "bg-primary";
}

function recentMonths(now: Date, count: number): string[] {
  const months: string[] = [];
  const base = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1),
  );
  for (let i = 0; i < count; i += 1) {
    const d = new Date(
      Date.UTC(base.getUTCFullYear(), base.getUTCMonth() - i, 1),
    );
    const yyyy = d.getUTCFullYear();
    const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
    months.push(`${yyyy}-${mm}`);
  }
  return months;
}

function isSecretProvider(provider: LlmProvider): provider is LlmSecretProvider {
  return provider === "openai" || provider === "anthropic";
}

function isAuthUnsupported(
  config: LlmProviderConfig | null,
  mode: LlmAuthMode,
): boolean {
  return Boolean(config?.unsupported_auth_modes?.includes(mode));
}

export default function LlmPage(): React.JSX.Element {
  const queryClient = useQueryClient();

  const monthOptions = React.useMemo(() => recentMonths(new Date(), 12), []);
  const [selectedMonth, setSelectedMonth] = React.useState<string>(
    monthOptions[0] ?? "",
  );

  const [provider, setSelectedProvider] = React.useState<LlmProvider | null>(
    null,
  );
  const [authMode, setSelectedAuthMode] = React.useState<LlmAuthMode | null>(
    null,
  );
  const [model, setSelectedModel] = React.useState<string | null>(null);
  const [apiKeyDraft, setApiKeyDraft] = React.useState("");
  const [hydratedFromActive, setHydratedFromActive] = React.useState(false);

  const settingsQuery = useQuery<LlmSettings>({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: getLlmSettings,
  });

  const usageQuery = useQuery<LlmUsage>({
    queryKey: ["llm", "usage", selectedMonth],
    queryFn: () => getUsage(selectedMonth || undefined),
  });

  const providers = React.useMemo(
    () => settingsQuery.data?.providers ?? [],
    [settingsQuery.data],
  );
  const active = settingsQuery.data?.active ?? null;

  const effectiveProvider = provider ?? active?.provider ?? "";
  const effectiveAuthMode = authMode ?? active?.auth_mode ?? "";
  const effectiveModel = model ?? active?.model ?? "";

  // GET 到着後、 active を初期選択にする (配列先頭 openai にはフォールバックしない)。
  // 不整合 model もそのまま hydrate し、 models[0] へ silent 置換しない。
  React.useEffect(() => {
    if (hydratedFromActive || !active) {
      return;
    }
    setSelectedProvider(active.provider);
    setSelectedAuthMode(active.auth_mode);
    setSelectedModel(active.model);
    setHydratedFromActive(true);
  }, [active, hydratedFromActive]);

  const selectedConfig = React.useMemo(
    () => providers.find((p) => p.provider === effectiveProvider) ?? null,
    [providers, effectiveProvider],
  );

  const modelMismatch = Boolean(
    selectedConfig &&
      effectiveModel &&
      !selectedConfig.models.includes(effectiveModel),
  );

  const invalidateSettings = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: SETTINGS_QUERY_KEY });

  const saveMutation = useMutation({
    mutationFn: ({
      provider: p,
      authMode: a,
      model: m,
    }: {
      provider: LlmProvider;
      authMode: LlmAuthMode;
      model: string;
    }) => setProvider(p, a, m),
    onSuccess: () => {
      void invalidateSettings();
    },
  });

  const credentialMutation = useMutation({
    mutationFn: ({
      provider: p,
      apiKey,
    }: {
      provider: LlmSecretProvider;
      apiKey: string;
    }) => setCredential(p, apiKey),
    onSuccess: () => {
      setApiKeyDraft("");
      void invalidateSettings();
    },
  });

  const clearCredentialMutation = useMutation({
    mutationFn: (p: LlmSecretProvider) => clearCredential(p),
    onSuccess: () => {
      void invalidateSettings();
    },
  });

  const handleProviderChange = React.useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>): void => {
      const next = event.target.value as LlmProvider;
      setSelectedProvider(next);
      setApiKeyDraft("");
      const cfg = providers.find((p) => p.provider === next);
      if (cfg) {
        const nextAuth =
          cfg.auth_modes.find((m) => !isAuthUnsupported(cfg, m)) ??
          cfg.auth_modes[0] ??
          "api_key";
        setSelectedAuthMode(nextAuth);
        setSelectedModel(cfg.models[0] ?? null);
      }
    },
    [providers],
  );

  const handleAuthModeChange = React.useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>): void => {
      setSelectedAuthMode(event.target.value as LlmAuthMode);
    },
    [],
  );

  const handleModelChange = React.useCallback(
    (event: React.ChangeEvent<HTMLSelectElement>): void => {
      setSelectedModel(event.target.value);
    },
    [],
  );

  const handleSave = React.useCallback((): void => {
    if (!effectiveProvider || !effectiveAuthMode || !effectiveModel) {
      return;
    }
    if (isAuthUnsupported(selectedConfig, effectiveAuthMode as LlmAuthMode)) {
      return;
    }
    saveMutation.mutate({
      provider: effectiveProvider as LlmProvider,
      authMode: effectiveAuthMode as LlmAuthMode,
      model: effectiveModel,
    });
  }, [effectiveProvider, effectiveAuthMode, effectiveModel, selectedConfig, saveMutation]);

  const showApiKeyForm =
    effectiveProvider !== "" &&
    isSecretProvider(effectiveProvider as LlmProvider) &&
    effectiveAuthMode === "api_key" &&
    selectedConfig !== null &&
    selectedConfig.credential_source !== "env" &&
    selectedConfig.credential_source !== "n/a";

  const envCredentialLocked =
    selectedConfig?.credential_source === "env" &&
    effectiveAuthMode === "api_key" &&
    effectiveProvider !== "" &&
    isSecretProvider(effectiveProvider as LlmProvider);

  const saveError = saveMutation.error;
  const saveErrorMessage =
    saveError instanceof ApiError &&
    (saveError.status === 400 || saveError.status === 422)
      ? saveError.message || "選択した設定はこの provider では使用できません。"
      : saveError
        ? "provider の保存に失敗しました。 時間をおいて再試行してください。"
        : null;

  const credentialError = credentialMutation.error ?? clearCredentialMutation.error;
  const credentialErrorMessage =
    credentialError instanceof ApiError && credentialError.status === 409
      ? "環境変数で設定済み（環境変数を使用）。DB への保存・削除はできません。"
      : credentialError instanceof ApiError && credentialError.status === 422
        ? "API キーは空にできません。"
        : credentialError
          ? "API キーの更新に失敗しました。"
          : null;

  const usage = usageQuery.data;
  const budgetPct = usage ? clampPct(usage.budget_pct) : 0;
  const byProviderRows = usage
    ? Object.entries(usage.by_provider).sort(
        (a, b) => b[1].cost_usd - a[1].cost_usd,
      )
    : [];

  const availableModels = selectedConfig?.models ?? [];
  const modelIsValid =
    effectiveModel !== "" && availableModels.includes(effectiveModel);
  const authUnsupported =
    effectiveAuthMode !== "" &&
    isAuthUnsupported(selectedConfig, effectiveAuthMode as LlmAuthMode);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <BrainCircuit className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">LLM 設定</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>現在のプロバイダ</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {settingsQuery.isLoading && (
            <p className="text-sm text-slate-500">読み込み中…</p>
          )}
          {settingsQuery.isError && (
            <p className="text-sm text-slate-500">
              backend に接続できません(未接続)。
            </p>
          )}
          {active ? (
            <div
              data-testid="llm-active-summary"
              className="flex flex-wrap items-center gap-3 text-sm"
            >
              <Badge variant="active" data-testid="llm-active-provider">
                {PROVIDER_LABELS[active.provider]}
              </Badge>
              <span className="text-slate-400">認証:</span>
              <span
                data-testid="llm-active-auth"
                className="font-mono text-slate-200"
              >
                {AUTH_MODE_LABELS[active.auth_mode]}
              </span>
              <span className="text-slate-400">モデル:</span>
              <span
                data-testid="llm-active-model"
                className="font-mono text-slate-200"
              >
                {active.model}
              </span>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>プロバイダ切替</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {!settingsQuery.isLoading &&
            !settingsQuery.isError &&
            providers.length === 0 && (
              <p className="text-sm text-slate-500">
                利用可能な provider がありません。
              </p>
            )}

          {!settingsQuery.isLoading &&
            !settingsQuery.isError &&
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
                    <NativeSelect
                      id="llm-provider-select"
                      data-testid="llm-provider-select"
                      value={effectiveProvider}
                      onChange={handleProviderChange}
                      disabled={saveMutation.isPending}
                    >
                      {providers.map((cfg) => (
                        <NativeSelectOption
                          key={cfg.provider}
                          value={cfg.provider}
                          muted={!cfg.available}
                          data-current={
                            cfg.provider === active?.provider
                              ? "true"
                              : undefined
                          }
                        >
                          {PROVIDER_LABELS[cfg.provider]}
                          {cfg.provider === active?.provider
                            ? "（現在）"
                            : ""}
                          {!cfg.available ? "（未設定）" : ""}
                        </NativeSelectOption>
                      ))}
                    </NativeSelect>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label
                      htmlFor="llm-authmode-select"
                      className="text-xs font-medium text-slate-400"
                    >
                      認証方式
                    </label>
                    <NativeSelect
                      id="llm-authmode-select"
                      data-testid="llm-authmode-select"
                      value={effectiveAuthMode}
                      onChange={handleAuthModeChange}
                      disabled={saveMutation.isPending || !selectedConfig}
                    >
                      {(selectedConfig?.auth_modes ?? []).map((mode) => (
                        <NativeSelectOption
                          key={mode}
                          value={mode}
                          disabled={isAuthUnsupported(selectedConfig, mode)}
                        >
                          {AUTH_MODE_LABELS[mode]}
                        </NativeSelectOption>
                      ))}
                    </NativeSelect>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label
                      htmlFor="llm-model-select"
                      className="text-xs font-medium text-slate-400"
                    >
                      モデル
                    </label>
                    <NativeSelect
                      id="llm-model-select"
                      data-testid="llm-model-select"
                      value={effectiveModel}
                      onChange={handleModelChange}
                      disabled={saveMutation.isPending || !selectedConfig}
                    >
                      {modelMismatch ? (
                        <NativeSelectOption value={effectiveModel} muted>
                          {effectiveModel}（無効・要修正）
                        </NativeSelectOption>
                      ) : null}
                      {availableModels.length > 0 ? (
                        availableModels.map((m) => (
                          <NativeSelectOption key={m} value={m}>
                            {m}
                          </NativeSelectOption>
                        ))
                      ) : (
                        <NativeSelectOption value="" disabled muted>
                          —
                        </NativeSelectOption>
                      )}
                    </NativeSelect>
                  </div>
                </div>

                {selectedConfig ? (
                  <p
                    data-testid="llm-credential-source"
                    className="text-xs text-slate-400"
                  >
                    資格情報ソース:{" "}
                    {SOURCE_LABELS[selectedConfig.credential_source]}
                    {selectedConfig.provider === "ollama"
                      ? ` / 既定モデル ${selectedConfig.models[0] ?? "—"}`
                      : null}
                  </p>
                ) : null}

                {modelMismatch ? (
                  <p
                    data-testid="llm-model-mismatch-notice"
                    className="text-sm text-amber-400"
                  >
                    保存済みのプロバイダとモデルの組合せが不正です。有効なモデルを選んで保存してください。
                  </p>
                ) : null}

                {authUnsupported ? (
                  <p
                    data-testid="llm-codex-unsupported"
                    className="text-sm text-amber-400"
                  >
                    Codex OAuth は未実装のため選択・保存できません。API
                    キー認証を使用してください。
                  </p>
                ) : null}

                {envCredentialLocked ? (
                  <p
                    data-testid="llm-env-credential-notice"
                    className="text-sm text-slate-300"
                  >
                    環境変数で設定済み（環境変数を使用）
                  </p>
                ) : null}

                {showApiKeyForm &&
                effectiveProvider &&
                isSecretProvider(effectiveProvider as LlmProvider) ? (
                  <div className="flex flex-col gap-2 rounded-lg border border-white/10 p-3">
                    <label
                      htmlFor="llm-api-key-input"
                      className="text-xs font-medium text-slate-400"
                    >
                      API キー（write-only・表示されません）
                    </label>
                    <Input
                      id="llm-api-key-input"
                      data-testid="llm-api-key-input"
                      type="password"
                      autoComplete="off"
                      maxLength={LLM_API_KEY_MAX_LENGTH}
                      value={apiKeyDraft}
                      onChange={(event) => setApiKeyDraft(event.target.value)}
                      placeholder={
                        selectedConfig?.credential_source === "db"
                          ? "新しいキーで置換"
                          : "API キーを入力"
                      }
                      disabled={
                        credentialMutation.isPending ||
                        clearCredentialMutation.isPending
                      }
                    />
                    <div className="flex flex-wrap gap-2">
                      <Button
                        data-testid="llm-credential-save-btn"
                        disabled={
                          credentialMutation.isPending ||
                          !apiKeyDraft.trim()
                        }
                        onClick={() =>
                          credentialMutation.mutate({
                            provider: effectiveProvider as LlmSecretProvider,
                            apiKey: apiKeyDraft.trim(),
                          })
                        }
                      >
                        {credentialMutation.isPending
                          ? "保存中…"
                          : "API キーを保存"}
                      </Button>
                      {selectedConfig?.credential_source === "db" ? (
                        <Button
                          data-testid="llm-credential-clear-btn"
                          variant="outline"
                          disabled={clearCredentialMutation.isPending}
                          onClick={() =>
                            clearCredentialMutation.mutate(
                              effectiveProvider as LlmSecretProvider,
                            )
                          }
                        >
                          DB キーを削除
                        </Button>
                      ) : null}
                    </div>
                    {credentialMutation.isSuccess &&
                    !credentialMutation.isPending ? (
                      <span className="text-xs text-primary">
                        API キーを保存しました（内容は表示しません）。
                      </span>
                    ) : null}
                    {credentialErrorMessage ? (
                      <p
                        data-testid="llm-credential-error"
                        className="text-sm text-danger"
                      >
                        {credentialErrorMessage}
                      </p>
                    ) : null}
                  </div>
                ) : null}

                {selectedConfig && !selectedConfig.available ? (
                  <p className="text-xs text-amber-400">
                    この provider は API キー/接続が未設定です。
                    保存しても呼び出しは失敗します。
                  </p>
                ) : null}

                <div className="flex items-center gap-3">
                  <Button
                    data-testid="llm-save-btn"
                    disabled={
                      saveMutation.isPending ||
                      !effectiveProvider ||
                      !effectiveAuthMode ||
                      !modelIsValid ||
                      authUnsupported
                    }
                    onClick={handleSave}
                  >
                    {saveMutation.isPending ? "保存中…" : "保存"}
                  </Button>
                  {saveMutation.isSuccess && !saveMutation.isPending ? (
                    <span className="text-xs text-primary">保存しました。</span>
                  ) : null}
                </div>
                <p className="text-xs text-slate-500">
                  切替は audit_log に記録されます。
                </p>

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
            <NativeSelect
              data-testid="llm-usage-month-select"
              value={selectedMonth}
              onChange={(event) => setSelectedMonth(event.target.value)}
              className="h-8 w-36 text-xs"
            >
              {monthOptions.map((m) => (
                <NativeSelectOption key={m} value={m}>
                  {m}
                </NativeSelectOption>
              ))}
            </NativeSelect>
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
