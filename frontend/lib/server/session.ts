// 単一 admin 向け署名付きセッション cookie(ADR-0013)。
// Edge middleware / Node Route Handler 双方で動くよう Web Crypto HMAC-SHA256 を使う。
// Cookie: HttpOnly / SameSite=Lax / Secure は AUTH_COOKIE_SECURE または HTTPS 時。

export const SESSION_COOKIE_NAME = "ymg_session";

/** セッション寿命(秒)。 LAN 単一 admin 向けに 8 時間。 */
export const SESSION_MAX_AGE_SEC = 8 * 60 * 60;

/** AUTH_SESSION_SECRET の最小長(UTF-8 バイト)。 */
export const MIN_SESSION_SECRET_BYTES = 32;

export interface SessionPayload {
  readonly username: string;
  /** Unix epoch seconds */
  readonly exp: number;
}

function getSecret(): string {
  const secret = process.env.AUTH_SESSION_SECRET;
  if (!secret) {
    throw new Error("AUTH_SESSION_SECRET is not configured");
  }
  const bytes = new TextEncoder().encode(secret).byteLength;
  if (bytes < MIN_SESSION_SECRET_BYTES) {
    throw new Error(
      `AUTH_SESSION_SECRET must be at least ${MIN_SESSION_SECRET_BYTES} UTF-8 bytes (got ${bytes})`,
    );
  }
  return secret;
}

function bytesToBase64Url(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (const b of view) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const padLen = (4 - (padded.length % 4)) % 4;
  const b64 = padded + "=".repeat(padLen);
  const binary = atob(b64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

async function signBody(body: string, secret: string): Promise<string> {
  const key = await hmacKey(secret);
  const sig = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(body),
  );
  return bytesToBase64Url(sig);
}

async function verifyBody(
  body: string,
  signature: string,
  secret: string,
): Promise<boolean> {
  const key = await hmacKey(secret);
  const sigBytes = base64UrlToBytes(signature);
  // Edge/Node 双方で timing-safe な verify を使う。
  return crypto.subtle.verify(
    "HMAC",
    key,
    sigBytes,
    new TextEncoder().encode(body),
  );
}

/** payload → `base64url(json).base64url(hmac)` トークン。 */
export async function createSessionToken(
  username: string,
  nowSec: number = Math.floor(Date.now() / 1000),
): Promise<string> {
  const payload: SessionPayload = {
    username,
    exp: nowSec + SESSION_MAX_AGE_SEC,
  };
  const body = bytesToBase64Url(
    new TextEncoder().encode(JSON.stringify(payload)),
  );
  const sig = await signBody(body, getSecret());
  return `${body}.${sig}`;
}

/** トークンを検証し、有効なら payload を返す。改ざん・期限切れ・不正形式は null(例外を投げない)。 */
export async function verifySessionToken(
  token: string | undefined | null,
  nowSec: number = Math.floor(Date.now() / 1000),
): Promise<SessionPayload | null> {
  if (!token) {
    return null;
  }
  try {
    const parts = token.split(".");
    if (parts.length !== 2) {
      return null;
    }
    const [body, sig] = parts;
    if (!body || !sig) {
      return null;
    }
    let secret: string;
    try {
      secret = getSecret();
    } catch {
      return null;
    }
    // base64/署名の不正は verifyBody / atob が投げうる → 外側で null。
    const ok = await verifyBody(body, sig, secret);
    if (!ok) {
      return null;
    }
    const json = new TextDecoder().decode(base64UrlToBytes(body));
    const payload = JSON.parse(json) as SessionPayload;
    if (
      typeof payload.username !== "string" ||
      typeof payload.exp !== "number" ||
      !payload.username ||
      payload.exp <= nowSec
    ) {
      return null;
    }
    return payload;
  } catch {
    return null;
  }
}

/** Set-Cookie 用の Secure 判定。 LAN HTTP では AUTH_COOKIE_SECURE=false。 */
export function sessionCookieSecure(): boolean {
  if (process.env.AUTH_COOKIE_SECURE === "true") {
    return true;
  }
  if (process.env.AUTH_COOKIE_SECURE === "false") {
    return false;
  }
  return process.env.NODE_ENV === "production";
}

export function sessionCookieOptions(): {
  httpOnly: true;
  secure: boolean;
  sameSite: "lax";
  path: "/";
  maxAge: number;
} {
  return {
    httpOnly: true,
    secure: sessionCookieSecure(),
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_MAX_AGE_SEC,
  };
}
