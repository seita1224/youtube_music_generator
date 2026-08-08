import { ScreenPlaceholder } from "@/components/admin/screen-placeholder";

// 編成表(ホーム、 ADR-0041): 週間カレンダー。 枠の色 = 制作ラインの現在地。
// 公開枠パターンの編集もこの画面で行う。

export default function SchedulePage(): React.JSX.Element {
  return (
    <ScreenPlaceholder
      title="編成表"
      description="週間の公開枠と制作ラインの現在地。公開枠パターンの編集もここで行う。"
    />
  );
}
