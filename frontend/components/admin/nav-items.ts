import {
  LayoutDashboard,
  ListChecks,
  ClipboardCheck,
  CalendarClock,
  BarChart3,
  Activity,
  BrainCircuit,
  FileText,
  Music2,
  Settings,
  type LucideIcon,
} from "lucide-react";

// canonical サイドバー(screen-spec.md §1)。
// 10 項目固定 + セパレータ + 設定(無効)。 他項目の追加は禁止。
// 「投稿 / Posts / 新規ジョブ作成 / サポート / MVP チェック」等は含めない。

export interface NavItem {
  readonly label: string;
  readonly href: string;
  readonly icon: LucideIcon;
  /** 将来用・初期は無効化(クリック不可)。 */
  readonly disabled?: boolean;
}

// 上段 9 項目(canonical 並び順、 screen-spec.md §1 のリストと一致)。
export const PRIMARY_NAV_ITEMS: readonly NavItem[] = [
  { label: "ダッシュボード", href: "/", icon: LayoutDashboard },
  { label: "プラン", href: "/plans", icon: ListChecks },
  { label: "Dryrun 審査", href: "/dryrun", icon: ClipboardCheck },
  { label: "スケジューラ", href: "/scheduler", icon: CalendarClock },
  { label: "分析", href: "/analytics", icon: BarChart3 },
  { label: "ジョブ進捗", href: "/jobs", icon: Activity },
  { label: "LLM", href: "/llm", icon: BrainCircuit },
  { label: "プロンプト", href: "/prompts", icon: FileText },
  { label: "ジャンル", href: "/genres", icon: Music2 },
];

// セパレータ下の項目(将来用、 初期は無効化)。
export const SECONDARY_NAV_ITEMS: readonly NavItem[] = [
  { label: "設定", href: "/settings", icon: Settings, disabled: true },
];
