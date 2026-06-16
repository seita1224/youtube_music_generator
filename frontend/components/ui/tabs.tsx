"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

// shadcn/ui 風 Tabs(radix 非依存、 React context ベースの自前実装)。
// 制御コンポーネント: value / onValueChange を呼び出し側が保持する。
// dryrun 一覧の state フィルタ(保留中 / 承認済 ...)に使用。

interface TabsContextValue {
  readonly value: string;
  readonly onValueChange: (value: string) => void;
}

const TabsContext = React.createContext<TabsContextValue | null>(null);

function useTabsContext(): TabsContextValue {
  const context = React.useContext(TabsContext);
  if (!context) {
    throw new Error("Tabs 系コンポーネントは <Tabs> の内側で使う必要があります");
  }
  return context;
}

interface TabsProps extends React.HTMLAttributes<HTMLDivElement> {
  readonly value: string;
  readonly onValueChange: (value: string) => void;
}

function Tabs({
  value,
  onValueChange,
  className,
  children,
  ...props
}: TabsProps): React.JSX.Element {
  const contextValue = React.useMemo<TabsContextValue>(
    () => ({ value, onValueChange }),
    [value, onValueChange],
  );
  return (
    <TabsContext.Provider value={contextValue}>
      <div className={cn("flex flex-col gap-4", className)} {...props}>
        {children}
      </div>
    </TabsContext.Provider>
  );
}

function TabsList({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): React.JSX.Element {
  return (
    <div
      role="tablist"
      className={cn(
        "inline-flex items-center gap-1 rounded-lg border border-white/10 bg-white/5 p-1",
        className,
      )}
      {...props}
    />
  );
}

interface TabsTriggerProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  readonly value: string;
}

const TabsTrigger = React.forwardRef<HTMLButtonElement, TabsTriggerProps>(
  ({ value, className, type = "button", ...props }, ref) => {
    const context = useTabsContext();
    const isActive = context.value === value;
    return (
      <button
        ref={ref}
        type={type}
        role="tab"
        aria-selected={isActive}
        onClick={() => context.onValueChange(value)}
        className={cn(
          "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
          isActive
            ? "bg-primary text-white"
            : "text-slate-400 hover:text-slate-200",
          className,
        )}
        {...props}
      />
    );
  },
);
TabsTrigger.displayName = "TabsTrigger";

interface TabsContentProps extends React.HTMLAttributes<HTMLDivElement> {
  readonly value: string;
}

function TabsContent({
  value,
  className,
  children,
  ...props
}: TabsContentProps): React.JSX.Element | null {
  const context = useTabsContext();
  if (context.value !== value) {
    return null;
  }
  return (
    <div role="tabpanel" className={cn("flex flex-col gap-4", className)} {...props}>
      {children}
    </div>
  );
}

export { Tabs, TabsList, TabsTrigger, TabsContent };
export type { TabsProps, TabsTriggerProps, TabsContentProps };
