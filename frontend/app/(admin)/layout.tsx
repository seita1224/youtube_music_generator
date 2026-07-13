import type { ReactNode } from "react";
import { cookies } from "next/headers";

import { Sidebar } from "@/components/admin/sidebar";
import { Header } from "@/components/admin/header";
import { QueryProvider } from "@/components/admin/query-provider";
import {
  SESSION_COOKIE_NAME,
  verifySessionToken,
} from "@/lib/server/session";

// T064: admin レイアウト(screen-spec.md §1)。
// canonical サイドバー(260px・左固定)+ canonical ヘッダー(64px・backdrop-blur)。
// 全 (admin) 配下画面で共通。 ルートレイアウト app/layout.tsx は Phase 1 既存。

export default async function AdminLayout({
  children,
}: Readonly<{ children: ReactNode }>): Promise<React.JSX.Element> {
  const jar = await cookies();
  const session = await verifySessionToken(
    jar.get(SESSION_COOKIE_NAME)?.value,
  );
  const username = session?.username ?? "admin";

  return (
    <QueryProvider>
      <div className="min-h-screen">
        <Sidebar username={username} />
        <div className="pl-[260px]">
          <Header username={username} />
          <main className="p-6">{children}</main>
        </div>
      </div>
    </QueryProvider>
  );
}
