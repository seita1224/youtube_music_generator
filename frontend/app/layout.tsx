import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "YMG — YouTube 音楽投稿自動化",
  description: "管理 UI (LAN 内 admin, セッション認証)",
};

// ルートレイアウト。 admin レイアウト/サイドバーは Phase 2 (T064) で (admin)/layout.tsx に実装。
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ja" className="dark">
      <body>{children}</body>
    </html>
  );
}
