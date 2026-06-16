"use client";

import * as React from "react";

import { authFetch } from "@/lib/auth";

// Basic 認証必須の backend メディア(動画/サムネ)を authFetch で取得し blob object URL 化する。
// frontend ページ自体は Basic 認証下に無く、 ブラウザは資格情報を持たないため、 素の
// <img src>/<video src> は 401 になる。 authFetch(NEXT_PUBLIC 資格情報を注入)で取得し
// object URL を src に渡すことで認証付きで表示する。 path=null の間は取得しない。

/**
 * 認証付きで取得したメディアの object URL を返す。
 * path が null の間、 取得失敗時は null(呼び出し側はプレースホルダにフォールバック)。
 * path 変更/アンマウント時に revokeObjectURL でクリーンアップする。
 */
export function useAuthedBlobUrl(path: string | null): string | null {
  const [url, setUrl] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!path) {
      setUrl(null);
      return;
    }
    let cancelled = false;
    let objectUrl: string | null = null;

    void (async () => {
      try {
        const resp = await authFetch(path);
        if (!resp.ok || cancelled) {
          return;
        }
        const blob = await resp.blob();
        if (cancelled) {
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      } catch {
        // ネットワーク/認証失敗時は null のまま。 呼び出し側がフォールバック表示する。
      }
    })();

    return () => {
      cancelled = true;
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
      setUrl(null);
    };
  }, [path]);

  return url;
}
