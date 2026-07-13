import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";

vi.mock("@/lib/api/llm", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/llm")>(
    "@/lib/api/llm",
  );
  return {
    ...actual,
    getLlmSettings: vi.fn(),
    getUsage: vi.fn(),
    setProvider: vi.fn(),
    setCredential: vi.fn(),
    clearCredential: vi.fn(),
  };
});

import LlmPage from "@/app/(admin)/llm/page";
import {
  getLlmSettings,
  getUsage,
  LLM_API_KEY_MAX_LENGTH,
  setCredential,
} from "@/lib/api/llm";

const getLlmSettingsMock = vi.mocked(getLlmSettings);
const getUsageMock = vi.mocked(getUsage);
const setCredentialMock = vi.mocked(setCredential);

const OLLAMA_SETTINGS = {
  active: {
    provider: "ollama" as const,
    auth_mode: "api_key" as const,
    model: "qwen2.5:3b",
  },
  providers: [
    {
      provider: "ollama" as const,
      available: true,
      auth_modes: ["api_key" as const],
      models: ["qwen2.5:3b", "llama3.2:3b"],
      credential_source: "n/a" as const,
      credential_configured: true,
      unsupported_auth_modes: [],
    },
  ],
};

const OPENAI_NONE_SETTINGS = {
  active: {
    provider: "openai" as const,
    auth_mode: "api_key" as const,
    model: "gpt-4.1",
  },
  providers: [
    {
      provider: "openai" as const,
      available: false,
      auth_modes: ["api_key" as const, "codex_oauth" as const],
      models: ["gpt-4.1", "gpt-4.1-mini"],
      credential_source: "none" as const,
      credential_configured: false,
      unsupported_auth_modes: ["codex_oauth" as const],
    },
    {
      provider: "ollama" as const,
      available: true,
      auth_modes: ["api_key" as const],
      models: ["qwen2.5:3b"],
      credential_source: "n/a" as const,
      credential_configured: true,
      unsupported_auth_modes: [],
    },
  ],
};

function renderPage(): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <LlmPage />
    </QueryClientProvider>,
  );
}

describe("LlmPage hydration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getUsageMock.mockResolvedValue({
      month: "2026-07",
      total_cost_usd: 0,
      budget_usd: 50,
      budget_pct: 0,
      by_provider: {},
    });
  });

  it("GET active 到着後すぐ select が Ollama/qwen を表示する", async () => {
    getLlmSettingsMock.mockResolvedValue(OLLAMA_SETTINGS);
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("llm-provider-select")).toHaveValue("ollama");
    });
    expect(screen.getByTestId("llm-model-select")).toHaveValue("qwen2.5:3b");
    expect(screen.getByTestId("llm-authmode-select")).toHaveValue("api_key");
  });

  it("不整合 active.model を models[0] に silent 置換せず、要修正 UI を出す", async () => {
    const { setProvider } = await import("@/lib/api/llm");
    const setProviderMock = vi.mocked(setProvider);
    setProviderMock.mockResolvedValue({
      provider: "ollama",
      auth_mode: "api_key",
      model: "qwen2.5:3b",
    });

    getLlmSettingsMock.mockResolvedValue({
      active: {
        provider: "ollama" as const,
        auth_mode: "api_key" as const,
        model: "gpt-4.1",
      },
      providers: [
        {
          provider: "ollama" as const,
          available: true,
          auth_modes: ["api_key" as const],
          models: ["qwen2.5:3b", "llama3.2:3b"],
          credential_source: "n/a" as const,
          credential_configured: true,
          unsupported_auth_modes: [],
        },
      ],
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("llm-provider-select")).toHaveValue("ollama");
    });
    expect(screen.getByTestId("llm-active-model")).toHaveTextContent("gpt-4.1");
    expect(screen.getByTestId("llm-model-select")).toHaveValue("gpt-4.1");
    expect(screen.getByTestId("llm-model-mismatch-notice")).toBeInTheDocument();
    expect(screen.getByTestId("llm-save-btn")).toBeDisabled();

    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(screen.getByTestId("llm-model-select"), {
      target: { value: "qwen2.5:3b" },
    });
    expect(screen.queryByTestId("llm-model-mismatch-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("llm-save-btn")).not.toBeDisabled();

    fireEvent.click(screen.getByTestId("llm-save-btn"));
    await waitFor(() => {
      expect(setProviderMock).toHaveBeenCalledWith(
        "ollama",
        "api_key",
        "qwen2.5:3b",
      );
    });
  });
});

describe("LlmPage API key input", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getUsageMock.mockResolvedValue({
      month: "2026-07",
      total_cost_usd: 0,
      budget_usd: 50,
      budget_pct: 0,
      by_provider: {},
    });
    setCredentialMock.mockResolvedValue({
      provider: "openai",
      credential_source: "db",
      credential_configured: true,
    });
  });

  it("password 入力に maxLength=2048 があり、送信前に trim する", async () => {
    getLlmSettingsMock.mockResolvedValue(OPENAI_NONE_SETTINGS);
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("llm-api-key-input")).toBeInTheDocument();
    });
    const input = screen.getByTestId("llm-api-key-input");
    expect(input).toHaveAttribute("maxLength", String(LLM_API_KEY_MAX_LENGTH));

    // fireEvent 相当: controlled input へ値を入れて保存クリック。
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(input, { target: { value: "  sk-ui-trimmed  " } });
    fireEvent.click(screen.getByTestId("llm-credential-save-btn"));

    await waitFor(() => {
      expect(setCredentialMock).toHaveBeenCalledWith("openai", "sk-ui-trimmed");
    });
  });
});
