import {
  CalendarDays,
  History,
  Music2,
  BarChart3,
  FileText,
  Settings,
  type LucideIcon,
} from "lucide-react";

// 枠中心モデルの画面構成(ADR-0041 (5))。
// 一覧 → 詳細のブレイクダウンが基本構造。 枠詳細 (/slots/[id]) は編成表からの
// ドリルダウンでありナビ項目に置かない。 旧画面(制作ラインボード・受信箱・
// 公開キュー・運営方針)に相当する項目は追加しない。

export interface NavItem {
  readonly label: string;
  readonly href: string;
  readonly icon: LucideIcon;
  readonly disabled?: boolean;
}

export const PRIMARY_NAV_ITEMS: readonly NavItem[] = [
  { label: "編成表", href: "/", icon: CalendarDays },
  { label: "タイムライン", href: "/timeline", icon: History },
  { label: "ジャンル", href: "/genres", icon: Music2 },
  { label: "分析", href: "/analytics", icon: BarChart3 },
  { label: "プロンプト", href: "/prompts", icon: FileText },
];

export const SECONDARY_NAV_ITEMS: readonly NavItem[] = [
  { label: "設定", href: "/settings", icon: Settings },
];
