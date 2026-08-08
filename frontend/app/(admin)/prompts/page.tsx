import { ScreenPlaceholder } from "@/components/admin/screen-placeholder";

// プロンプト(ADR-0041): 工程 1:1 のプロンプト管理 + 枠に影響しない「試しに 1 件生成」。

export default function PromptsPage(): React.JSX.Element {
  return (
    <ScreenPlaceholder
      title="プロンプト"
      description="工程ごとのプロンプト管理と、枠に影響しない試し実行。"
    />
  );
}
