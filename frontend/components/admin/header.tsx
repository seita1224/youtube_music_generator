"use client";

import { Bell, Settings } from "lucide-react";
import { usePathname } from "next/navigation";

import { LogoutButton } from "@/components/admin/logout-button";
import { PRIMARY_NAV_ITEMS, SECONDARY_NAV_ITEMS } from "@/components/admin/nav-items";

// canonical ヘッダー(screen-spec.md §1)。 上部固定・高さ 64px・backdrop-blur。
// 左: 画面タイトル + サブテキスト / 右: 通知ベル + 設定アイコン + 「admin」表示。
// 複数ユーザー名(admin_suzuki 等)・アバター画像は禁止(単一ユーザー前提)。
// タイトルは現在ルート(usePathname)を nav-items に照合して自動決定する
// (詳細ルート /dryrun/[id] 等は prefix 一致で親項目に寄せる)。

const ALL_NAV_ITEMS = [...PRIMARY_NAV_ITEMS, ...SECONDARY_NAV_ITEMS];

// ルート別サブテキスト(無い画面は非表示)。
const SUBTITLES: Readonly<Record<string, string>> = {
  "/": "週間の公開枠と制作ラインの現在地",
};

const FALLBACK_TITLE = "管理コンソール";

/** pathname を nav-items に照合し画面タイトルを決める。 ルート "/" は完全一致のみ。 */
function titleForPath(pathname: string): string {
  const match = ALL_NAV_ITEMS.filter((item) =>
    item.href === "/"
      ? pathname === "/"
      : pathname === item.href || pathname.startsWith(`${item.href}/`),
  ).sort((a, b) => b.href.length - a.href.length)[0];
  return match?.label ?? FALLBACK_TITLE;
}

interface HeaderProps {
  readonly username?: string;
}

export function Header({
  username = "admin",
}: HeaderProps): React.JSX.Element {
  const pathname = usePathname() ?? "/";
  const title = titleForPath(pathname);
  const subtitle = SUBTITLES[pathname];

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
        <span
          className="text-sm font-medium text-slate-300"
          data-testid="header-username"
        >
          {username}
        </span>
        <LogoutButton
          className="text-sm text-slate-400 transition-colors hover:text-danger disabled:opacity-50"
          testId="header-logout"
        />
      </div>
    </header>
  );
}
