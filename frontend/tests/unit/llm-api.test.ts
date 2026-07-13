import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/client", () => ({
  apiGet: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}));

import { apiDelete, apiGet, apiPut } from "@/lib/api/client";
import {
  clearCredential,
  getLlmSettings,
  setCredential,
  setProvider,
} from "@/lib/api/llm";

const apiGetMock = vi.mocked(apiGet);
const apiPutMock = vi.mocked(apiPut);
const apiDeleteMock = vi.mocked(apiDelete);

describe("llm api client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("getLlmSettings は /llm/providers を GET する", async () => {
    const payload = {
      active: { provider: "ollama", auth_mode: "api_key", model: "qwen2.5:3b" },
      providers: [],
    };
    apiGetMock.mockResolvedValue(payload);

    await expect(getLlmSettings()).resolves.toEqual(payload);
    expect(apiGetMock).toHaveBeenCalledWith("/llm/providers");
  });

  it("setProvider は model を含めて PUT する", async () => {
    apiPutMock.mockResolvedValue({
      provider: "ollama",
      auth_mode: "api_key",
      model: "llama3.2:3b",
    });

    await setProvider("ollama", "api_key", "llama3.2:3b");
    expect(apiPutMock).toHaveBeenCalledWith("/llm/providers", {
      provider: "ollama",
      auth_mode: "api_key",
      model: "llama3.2:3b",
    });
  });

  it("setCredential / clearCredential は write-only 経路を呼ぶ", async () => {
    apiPutMock.mockResolvedValue({
      provider: "openai",
      credential_source: "db",
      credential_configured: true,
    });
    apiDeleteMock.mockResolvedValue({
      provider: "openai",
      credential_source: "none",
      credential_configured: false,
    });

    await setCredential("openai", "sk-test");
    expect(apiPutMock).toHaveBeenCalledWith("/llm/credentials", {
      provider: "openai",
      api_key: "sk-test",
    });

    await clearCredential("openai");
    expect(apiDeleteMock).toHaveBeenCalledWith("/llm/credentials/openai");
  });

  it("setCredential は送信前に api_key を trim する", async () => {
    apiPutMock.mockResolvedValue({
      provider: "openai",
      credential_source: "db",
      credential_configured: true,
    });

    await setCredential("openai", "  sk-padded  ");
    expect(apiPutMock).toHaveBeenCalledWith("/llm/credentials", {
      provider: "openai",
      api_key: "sk-padded",
    });
  });
});
