import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// 枠中心モデル(ADR-0041)の画面枠。 各画面の実装が入るまでの共通表示。

interface ScreenPlaceholderProps {
  readonly title: string;
  readonly description: string;
}

export function ScreenPlaceholder({
  title,
  description,
}: ScreenPlaceholderProps): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <p className="text-sm text-slate-400">{description}</p>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-slate-400">準備中</p>
      </CardContent>
    </Card>
  );
}
