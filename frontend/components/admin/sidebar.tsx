"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";
import { Separator } from "@/components/ui/separator";
import {
  PRIMARY_NAV_ITEMS,
  SECONDARY_NAV_ITEMS,
  type NavItem,
} from "@/components/admin/nav-items";

// canonical サイドバー(screen-spec.md §1)。 固定幅 260px・左固定。
// active: 左に Electric Purple 縦バー + 白文字 / inactive: slate-400。

/** href が現在のパスに対して active かを判定(`/` は完全一致、 他は前方一致)。 */
function isActive(pathname: string, href: string): boolean {
  if (href === "/") {
    return pathname === "/";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

interface NavLinkProps {
  readonly item: NavItem;
  readonly active: boolean;
}

function NavLink({ item, active }: NavLinkProps): React.JSX.Element {
  const Icon = item.icon;
  const base =
    "relative flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors";

  if (item.disabled) {
    return (
      <span
        aria-disabled="true"
        className={cn(base, "cursor-not-allowed text-slate-600")}
      >
        <Icon className="h-4 w-4" aria-hidden="true" />
        <span>{item.label}</span>
      </span>
    );
  }

  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        base,
        active
          ? "bg-white/5 font-medium text-white"
          : "text-slate-400 hover:bg-white/5 hover:text-slate-200",
      )}
    >
      {active && (
        <span
          aria-hidden="true"
          className="absolute left-0 top-1/2 h-5 w-1 -translate-y-1/2 rounded-r bg-primary"
        />
      )}
      <Icon className="h-4 w-4" aria-hidden="true" />
      <span>{item.label}</span>
    </Link>
  );
}

export function Sidebar(): React.JSX.Element {
  const pathname = usePathname();

  return (
    <aside className="fixed inset-y-0 left-0 flex w-[260px] flex-col border-r border-white/10 bg-black/30 backdrop-blur-md">
      <div className="px-5 py-5">
        <span className="text-lg font-semibold tracking-tight text-white">
          YMG
        </span>
      </div>

      <nav className="flex flex-1 flex-col gap-1 px-3" aria-label="メインナビゲーション">
        {PRIMARY_NAV_ITEMS.map((item) => (
          <NavLink key={item.href} item={item} active={isActive(pathname, item.href)} />
        ))}

        <Separator className="my-2" />

        {SECONDARY_NAV_ITEMS.map((item) => (
          <NavLink key={item.href} item={item} active={isActive(pathname, item.href)} />
        ))}
      </nav>

      <div className="border-t border-white/10 px-5 py-4">
        <div className="flex items-center justify-between text-sm">
          <span className="text-slate-300">admin</span>
          <a
            href="/api/backend/logout"
            className="text-slate-400 hover:text-danger"
            title="Basic 認証セッションを破棄(ベストエフォート)"
          >
            ログアウト
          </a>
        </div>
      </div>
    </aside>
  );
}
