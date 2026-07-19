import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";

describe("NativeSelect", () => {
  it("native focus/change semantics と dark style hook を維持する", () => {
    const onChange = vi.fn();
    render(
      <NativeSelect
        aria-label="Provider"
        defaultValue="ollama"
        onChange={onChange}
      >
        <NativeSelectOption value="openai" muted>
          OpenAI（未設定）
        </NativeSelectOption>
        <NativeSelectOption value="ollama" data-current="true">
          Ollama（現在）
        </NativeSelectOption>
      </NativeSelect>,
    );

    const select = screen.getByRole("combobox", { name: "Provider" });
    select.focus();
    expect(select).toHaveFocus();
    expect(select).toHaveClass("native-dark-select");

    fireEvent.change(select, { target: { value: "openai" } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(select).toHaveValue("openai");
  });

  it("unavailable は選択可能な muted、unsupported は disabled のままにする", () => {
    render(
      <NativeSelect aria-label="認証方式" defaultValue="api_key">
        <NativeSelectOption value="api_key" muted>
          API キー（未設定）
        </NativeSelectOption>
        <NativeSelectOption value="codex_oauth" disabled>
          Codex OAuth（未対応）
        </NativeSelectOption>
      </NativeSelect>,
    );

    const unavailable = screen.getByRole("option", {
      name: "API キー（未設定）",
    });
    const unsupported = screen.getByRole("option", {
      name: "Codex OAuth（未対応）",
    });

    expect(unavailable).toHaveAttribute("data-muted", "true");
    expect(unavailable).not.toBeDisabled();
    expect(unsupported).toBeDisabled();
    expect(unsupported).toHaveClass("native-dark-select-option");
  });
});
