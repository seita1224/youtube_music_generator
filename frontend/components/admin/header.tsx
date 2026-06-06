import { Bell, Settings } from "lucide-react";

// canonical ヘッダー(screen-spec.md §1)。 上部固定・高さ 64px・backdrop-blur。
// 左: 画面タイトル + サブテキスト / 右: 通知ベル + 設定アイコン + 「admin」表示。
// 複数ユーザー名(admin_suzuki 等)・アバター画像は禁止(単一ユーザー前提)。

interface HeaderProps {
  readonly title: string;
  readonly subtitle?: string;
}

export function Header({ title, subtitle }: HeaderProps): React.JSX.Element {
  return (
    <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-white/10 bg-black/30 px-6 backdrop-blur-md">
      <div className="flex flex-col">
        <h1 className="text-lg font-semibold tracking-tight text-white">{title}</h1>
        {subtitle && <p className="text-sm text-slate-400 opacity-60">{subtitle}</p>}
      </div>

      <div className="flex items-center gap-4">
        <button
          type="button"
          aria-label="通知"
          className="text-slate-400 transition-colors hover:text-slate-200"
        >
          <Bell className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          aria-label="設定"
          className="text-slate-400 transition-colors hover:text-slate-200"
        >
          <Settings className="h-4 w-4" aria-hidden="true" />
        </button>
        <span className="text-sm font-medium text-slate-300">admin</span>
        <a
          href="/api/backend/logout"
          className="text-sm text-slate-400 transition-colors hover:text-danger"
          title="Basic 認証セッションを破棄(ベストエフォート)"
        >
          ログアウト
        </a>
      </div>
    </header>
  );
}
