import { ScreenPlaceholder } from "@/components/admin/screen-placeholder";

// 分析(ADR-0047 / ADR-0048): 目的関数(週次総視聴時間 + 維持率ガードレール)と
// 視聴者の声(読み取り専用)。

export default function AnalyticsPage(): React.JSX.Element {
  return (
    <ScreenPlaceholder
      title="分析"
      description="週次総視聴時間と維持率ガードレール、視聴者の声(読み取り専用)。"
    />
  );
}
