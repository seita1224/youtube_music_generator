// ADMIN_* クレデンシャルの timing-safe 比較(ADR-0013)。
// 長さ差によるタイミング漏洩を避けるため、 SHA-256 ダイジェスト同士を比較する。

import { createHash, timingSafeEqual } from "node:crypto";

function digest(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

/** 定数時間で 2 文字列が一致するか。 */
export function timingSafeStringEqual(a: string, b: string): boolean {
  const da = digest(a);
  const db = digest(b);
  return timingSafeEqual(da, db);
}

export interface AdminCredentials {
  readonly username: string;
  readonly password: string;
}

/** サーバ専用 ADMIN_* を読む。未設定なら null。 */
export function readAdminCredentials(): AdminCredentials | null {
  const username = process.env.ADMIN_USERNAME;
  const password = process.env.ADMIN_PASSWORD;
  if (!username || !password) {
    return null;
  }
  return { username, password };
}

/**
 * 提出クレデンシャルが ADMIN_* と一致するか。
 * ユーザー名不一致でもパスワード比較を行い、応答時間差を抑える。
 */
export function verifyAdminCredentials(
  submittedUser: string,
  submittedPassword: string,
): boolean {
  const expected = readAdminCredentials();
  if (!expected) {
    return false;
  }
  const userOk = timingSafeStringEqual(submittedUser, expected.username);
  const passOk = timingSafeStringEqual(submittedPassword, expected.password);
  return userOk && passOk;
}

/** backend Basic Authorization ヘッダ値(サーバ専用、クライアントに出さない)。 */
export function backendBasicAuthorization(): string | null {
  const creds = readAdminCredentials();
  if (!creds) {
    return null;
  }
  const raw = `${creds.username}:${creds.password}`;
  return `Basic ${Buffer.from(raw, "utf8").toString("base64")}`;
}
