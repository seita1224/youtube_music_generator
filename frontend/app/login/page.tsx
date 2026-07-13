"use client";

import { useEffect, useState, Suspense, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { safeNextPath } from "@/lib/safe-next-path";

// 単一 admin ログイン画面(ADR-0013)。 資格情報はサーバで検証し、
// HttpOnly セッション cookie を発行する。 クライアントにパスワードを埋め込まない。
// method=post は hydration 前の native GET 送信(クエリにパスワードが載る事故)を防ぐ。

function LoginForm(): React.JSX.Element {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setHydrated(true);
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!hydrated) {
      return;
    }
    setError(null);
    setPending(true);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        const data = (await response.json().catch(() => null)) as {
          detail?: string;
        } | null;
        setError(data?.detail ?? "ログインに失敗しました");
        return;
      }
      const dest = safeNextPath(
        searchParams.get("next"),
        window.location.href,
      );
      router.replace(dest);
      router.refresh();
    } catch {
      setError("ログインに失敗しました");
    } finally {
      setPending(false);
    }
  }

  return (
    <form
      method="post"
      action="/login"
      onSubmit={onSubmit}
      className="w-full max-w-sm space-y-4 rounded-xl border border-white/10 bg-black/40 p-8 backdrop-blur-md"
      data-testid="login-form"
      data-hydrated={hydrated ? "true" : "false"}
    >
      <div className="space-y-1">
        <p className="text-2xl font-semibold tracking-tight text-white">YMG</p>
        <h1 className="text-sm text-slate-400">管理コンソールにログイン</h1>
      </div>

      <div className="space-y-2">
        <label htmlFor="username" className="text-sm text-slate-300">
          ユーザー名
        </label>
        <Input
          id="username"
          name="username"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
          data-testid="login-username"
        />
      </div>

      <div className="space-y-2">
        <label htmlFor="password" className="text-sm text-slate-300">
          パスワード
        </label>
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          data-testid="login-password"
        />
      </div>

      {error && (
        <p
          role="alert"
          className="text-sm text-danger"
          data-testid="login-error"
        >
          {error}
        </p>
      )}

      <Button
        type="submit"
        className="w-full"
        disabled={!hydrated || pending}
        data-testid="login-submit"
      >
        {!hydrated ? "準備中…" : pending ? "ログイン中…" : "ログイン"}
      </Button>
    </form>
  );
}

export default function LoginPage(): React.JSX.Element {
  return (
    <main className="flex min-h-screen items-center justify-center bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-primary/20 via-slate-950 to-black px-4">
      <Suspense fallback={<div className="text-slate-400">読み込み中…</div>}>
        <LoginForm />
      </Suspense>
    </main>
  );
}
