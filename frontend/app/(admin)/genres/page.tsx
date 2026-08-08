import { ScreenPlaceholder } from "@/components/admin/screen-placeholder";

// ジャンル(ADR-0041): ポートフォリオと role 遷移(運営判断、 ADR-0042)。

export default function GenresPage(): React.JSX.Element {
  return (
    <ScreenPlaceholder
      title="ジャンル"
      description="ポートフォリオと役割(主力 / 拡張 / 実験)の遷移。昇格・撤退は運営判断として記録される。"
    />
  );
}
