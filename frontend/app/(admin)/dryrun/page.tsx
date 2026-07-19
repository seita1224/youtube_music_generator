"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { ClipboardCheck, Film } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  dryrunThumbnailPath,
  listDryrunOutputs,
  type DryrunListResponse,
  type DryrunOutput,
  type DryrunState,
} from "@/lib/api/dryrun";
import { useAuthedBlobUrl } from "@/lib/api/use-authed-blob-url";

// T097: dryrun 一覧画面(screen-spec.md §2 ③ / UI 契約)。
// 状態タブで pending 等を切替し、 各 dryrun を card で並べる。
// サムネ枠 + 状態バッジ(dryrun-state-badge)+ 作成日時を表示し、
// card クリックで詳細 /dryrun/[id] へ遷移する(承認/却下は詳細側で実施)。
// backend 未接続時はクエリ失敗を握り潰さず「未接続」として明示する。

interface StateTab {
  readonly value: DryrunState;
  readonly label: string;
}

// UI 契約の状態ラベル。 タブ並びは審査フロー順(保留中→承認済→投稿済→却下→期限切れ)。
const STATE_TABS: readonly StateTab[] = [
  { value: "pending", label: "保留中" },
  { value: "approved", label: "承認済" },
  { value: "posted", label: "投稿済" },
  { value: "rejected", label: "却下" },
  { value: "auto_expired", label: "期限切れ" },
];

const STATE_LABELS: Readonly<Record<DryrunState, string>> = {
  pending: "保留中",
  approved: "承認済",
  rejected: "却下",
  auto_expired: "期限切れ",
  posted: "投稿済",
};

// 状態 → Badge variant。 pending=注目(active)、 終端の却下/期限切れ=danger、 他=muted。
const STATE_BADGE_VARIANT: Readonly<
  Record<DryrunState, "default" | "active" | "danger" | "muted">
> = {
  pending: "active",
  approved: "active",
  posted: "default",
  rejected: "danger",
  auto_expired: "muted",
};

/** ISO 文字列を `YYYY-MM-DD HH:mm` 風に整形。 不正値はそのまま返す。 */
function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export default function DryrunListPage(): React.JSX.Element {
  const [state, setState] = React.useState<DryrunState>("pending");

  const query = useQuery<DryrunListResponse>({
    queryKey: ["dryrun", "outputs", state],
    queryFn: () => listDryrunOutputs(state),
  });

  const handleValueChange = React.useCallback((value: string): void => {
    setState(value as DryrunState);
  }, []);

  const items = query.data?.items ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2">
        <ClipboardCheck className="h-5 w-5 text-primary" aria-hidden="true" />
        <h1 className="text-lg font-semibold text-slate-200">Dryrun 審査</h1>
      </div>

      <Tabs value={state} onValueChange={handleValueChange}>
        <TabsList>
          {STATE_TABS.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {query.isLoading && (
        <p className="text-sm text-slate-500">読み込み中…</p>
      )}

      {query.isError && (
        <p className="text-sm text-slate-500">
          backend に接続できません(未接続)。
        </p>
      )}

      {!query.isLoading && !query.isError && items.length === 0 && (
        <p className="text-sm text-slate-500">
          {STATE_LABELS[state]}の dryrun はありません。
        </p>
      )}

      {!query.isLoading && !query.isError && items.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {items.map((item) => (
            <DryrunCard key={item.id} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}

function DryrunCard({ item }: { readonly item: DryrunOutput }): React.JSX.Element {
  // サムネは Basic 認証必須のため authFetch→blob で取得(素の <img src> は 401)。
  const thumbUrl = useAuthedBlobUrl(
    item.has_thumbnail ? dryrunThumbnailPath(item.id) : null,
  );
  return (
    <a
      href={`/dryrun/${item.id}`}
      data-testid="dryrun-card"
      className="group block rounded-xl outline-none transition-colors focus-visible:ring-2 focus-visible:ring-primary"
    >
      <Card className="overflow-hidden transition-colors group-hover:border-primary/40">
        {/* サムネ枠: 認証付き取得できたら JPEG を表示。 取得前/未生成/削除済みは Film アイコン。 */}
        <div className="relative flex aspect-video items-center justify-center border-b border-white/10 bg-black/40">
          {thumbUrl ? (
            // eslint-disable-next-line @next/next/no-img-element -- 認証付き blob URL のため next/image 非対応
            <img
              src={thumbUrl}
              alt={item.title ?? "dryrun サムネイル"}
              className="h-full w-full object-cover"
            />
          ) : (
            <Film className="h-8 w-8 text-slate-600" aria-hidden="true" />
          )}
        </div>
        <CardContent className="flex flex-col gap-2 p-4">
          <div className="flex items-center justify-between gap-2">
            <Badge
              data-testid="dryrun-state-badge"
              variant={STATE_BADGE_VARIANT[item.state]}
            >
              {STATE_LABELS[item.state]}
            </Badge>
            <time
              dateTime={item.created_at}
              className="font-mono text-xs text-slate-500"
            >
              {formatDateTime(item.created_at)}
            </time>
          </div>
          {/* タイトル優先表示(UUID は識別しづらいため)。 未設定時は ID にフォールバック。 */}
          <p
            className="truncate text-sm font-medium text-slate-200"
            title={item.title ?? item.id}
          >
            {item.title ?? item.id}
          </p>
          {item.title ? (
            <p className="truncate font-mono text-xs text-slate-500" title={item.id}>
              {item.id}
            </p>
          ) : null}
        </CardContent>
      </Card>
    </a>
  );
}
