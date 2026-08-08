import { ScreenPlaceholder } from "@/components/admin/screen-placeholder";

// 枠詳細(ADR-0041): 枠 1 つの工程詳細。 各工程の「使った(入力)/ できた(出力)」、
// 工程単位のやり直し(ADR-0046)、 公開ゲート(ADR-0042)を持つドリルダウン画面。

export default async function SlotDetailPage({
  params,
}: Readonly<{ params: Promise<{ id: string }> }>): Promise<React.JSX.Element> {
  const { id } = await params;
  return (
    <ScreenPlaceholder
      title={`枠詳細 #${id}`}
      description="工程ごとの入出力・やり直し・公開ゲート。"
    />
  );
}
