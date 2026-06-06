import type { ReactNode } from "react";

import { Sidebar } from "@/components/admin/sidebar";
import { Header } from "@/components/admin/header";
import { QueryProvider } from "@/components/admin/query-provider";

// T064: admin レイアウト(screen-spec.md §1)。
// canonical サイドバー(260px・左固定)+ canonical ヘッダー(64px・backdrop-blur)。
// 全 (admin) 配下画面で共通。 ルートレイアウト app/layout.tsx は Phase 1 既存。

export default function AdminLayout({
  children,
}: Readonly<{ children: ReactNode }>): React.JSX.Element {
  return (
    <QueryProvider>
      <div className="min-h-screen">
        <Sidebar />
        <div className="pl-[260px]">
          <Header
            title="ダッシュボード"
            subtitle="YouTube 音楽投稿自動化システムの横断的な状態確認"
          />
          <main className="p-6">{children}</main>
        </div>
      </div>
    </QueryProvider>
  );
}
