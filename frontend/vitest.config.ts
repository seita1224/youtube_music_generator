import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `@/*` は tsconfig.json の paths(`@` = frontend ルート)と一致させる。
// テスト対象の lib コードが `@/lib/...` を import するため、 alias 解決が必須。
const root = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": resolve(root, "."),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["tests/unit/**/*.{test,spec}.{ts,tsx}"],
    passWithNoTests: true,
  },
});
