// 単一 Node プロセス向けのインメモリ login スロットリング。
// マルチインスタンスでは共有されない(LAN 単一 admin / 単一 frontend 前提)。
//
// クライアントが捏造できる X-Forwarded-For / X-Real-IP は信頼しない。
// 単一 admin 向けにグローバルバケットを使う。

interface AttemptBucket {
  count: number;
  resetAt: number;
}

const buckets = new Map<string, AttemptBucket>();

/** 既定: 15 分窓で 10 回まで。 */
export const LOGIN_WINDOW_MS = 15 * 60 * 1000;
export const LOGIN_MAX_ATTEMPTS = 10;

/**
 * ログイン失敗の共有キー。
 * プロキシ背後でクライアント IP を使う場合は、信頼できるプロキシ設定を別途導入する。
 */
export const GLOBAL_LOGIN_BUCKET_KEY = "global-login";

/** テスト用にバケットを空にする。 */
export function resetLoginRateLimit(): void {
  buckets.clear();
}

/**
 * キーが制限中なら true。
 * 窓を過ぎていればバケットを捨てて false。
 */
export function isLoginRateLimited(
  key: string,
  nowMs: number = Date.now(),
): boolean {
  const bucket = buckets.get(key);
  if (!bucket) {
    return false;
  }
  if (nowMs >= bucket.resetAt) {
    buckets.delete(key);
    return false;
  }
  return bucket.count >= LOGIN_MAX_ATTEMPTS;
}

/** 失敗試行を 1 加算する。成功時は clearLoginAttempts を呼ぶ。 */
export function recordLoginFailure(
  key: string,
  nowMs: number = Date.now(),
): void {
  const bucket = buckets.get(key);
  if (!bucket || nowMs >= bucket.resetAt) {
    buckets.set(key, { count: 1, resetAt: nowMs + LOGIN_WINDOW_MS });
    return;
  }
  bucket.count += 1;
}

export function clearLoginAttempts(key: string): void {
  buckets.delete(key);
}
