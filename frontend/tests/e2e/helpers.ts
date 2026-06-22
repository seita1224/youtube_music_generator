// E2E 共有ヘルパー。 testMatch(*.spec.ts)に該当しないため test として実行されない。
//
// LAN 内 Basic 認証(ADR-0013)。 env 優先、 未設定時はローカル backend 既定 admin/admin に
// フォールバックする(ソースに秘密はハードコードしない)。 backend はすべて page.route で
// モックするため、 実際の認証は通らないが、 ブラウザの Basic セッション付与のため指定する。

export const BASIC_AUTH = {
  username:
    process.env.E2E_BASIC_AUTH_USER ??
    process.env.NEXT_PUBLIC_BASIC_AUTH_USER ??
    "admin",
  password:
    process.env.E2E_BASIC_AUTH_PASSWORD ??
    process.env.NEXT_PUBLIC_BASIC_AUTH_PASSWORD ??
    "admin",
} as const;
