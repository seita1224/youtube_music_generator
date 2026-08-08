import { ScreenPlaceholder } from "@/components/admin/screen-placeholder";

// 設定(ADR-0041 / ADR-0043 / ADR-0044): 自動運転レベル / LLM / GPU / バックアップ。
// システムの状態は表示のみ(停止操作は CLI / Slack、 ADR-0044)。

export default function SettingsPage(): React.JSX.Element {
  return (
    <ScreenPlaceholder
      title="設定"
      description="自動運転レベル・LLM・GPU・バックアップ。システム状態は表示のみ(停止操作は CLI / Slack)。"
    />
  );
}
