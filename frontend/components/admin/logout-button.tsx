"use client";

import { useState } from "react";

interface LogoutButtonProps {
  readonly className?: string;
  readonly testId: string;
}

/**
 * POST + same-origin CSRF でセッションを破棄し /login へ遷移する。
 * GET ログアウトは CSRF 回避になり得るため使わない(ADR-0013)。
 */
export function LogoutButton({
  className,
  testId,
}: LogoutButtonProps): React.JSX.Element {
  const [pending, setPending] = useState(false);

  async function onClick(): Promise<void> {
    if (pending) {
      return;
    }
    setPending(true);
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
    } catch {
      // cookie 破棄に失敗してもログイン画面へ退避する
    } finally {
      window.location.assign("/login");
    }
  }

  return (
    <button
      type="button"
      onClick={() => {
        void onClick();
      }}
      disabled={pending}
      className={className}
      title="セッションを破棄してログイン画面へ"
      data-testid={testId}
    >
      ログアウト
    </button>
  );
}
