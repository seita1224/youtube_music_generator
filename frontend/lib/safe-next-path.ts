// ログイン後リダイレクト用の相対パス検証。
// オープンリダイレクト(protocol-relative / バックスラッシュ / 他 origin)を拒否する。

const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

/**
 * `next` クエリ等を同一オリジンの相対パスへ正規化する。
 * 不正・危険な候補は `fallback`(既定 `/`)を返す。
 */
export function safeNextPath(
  candidate: string | null | undefined,
  requestUrl: string | URL,
  fallback = "/",
): string {
  if (candidate == null || candidate === "") {
    return fallback;
  }

  let decoded: string;
  try {
    decoded = decodeURIComponent(candidate);
  } catch {
    return fallback;
  }

  if (CONTROL_CHARS.test(candidate) || CONTROL_CHARS.test(decoded)) {
    return fallback;
  }
  // `/\evil.com` / `/\\evil.com` および %5C 系を拒否。
  if (candidate.includes("\\") || decoded.includes("\\")) {
    return fallback;
  }
  if (!decoded.startsWith("/") || decoded.startsWith("//")) {
    return fallback;
  }

  try {
    const base =
      typeof requestUrl === "string" ? new URL(requestUrl) : new URL(requestUrl.href);
    const resolved = new URL(decoded, base);
    if (resolved.origin !== base.origin) {
      return fallback;
    }
    const path = `${resolved.pathname}${resolved.search}${resolved.hash}`;
    // 解決後も protocol-relative 相当になっていないこと。
    if (!path.startsWith("/") || path.startsWith("//")) {
      return fallback;
    }
    return path;
  } catch {
    return fallback;
  }
}
