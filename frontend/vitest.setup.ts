// vitest 共通セットアップ。 jest-dom マッチャ(toBeInTheDocument 等)を有効化し、
// 各テスト後に Testing Library の DOM をクリーンアップする(テスト間の状態漏れ防止)。

import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
