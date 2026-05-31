// Phase 1 スキャフォールドのプレースホルダ。
// 実際のダッシュボードは Phase 2 (T064 admin レイアウト + T067 ダッシュボード) で差し替える。
export default function Home() {
  return (
    <main className="flex min-h-screen items-center justify-center p-8">
      <div className="text-center">
        <h1 className="text-2xl font-bold text-primary">YMG 管理 UI</h1>
        <p className="mt-2 text-sm opacity-60">
          Phase 1 scaffold — ダッシュボードは Phase 2 で実装 (T064 / T067)
        </p>
      </div>
    </main>
  );
}
