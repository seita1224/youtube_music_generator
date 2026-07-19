"use client";

import * as React from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

// shadcn/ui 風 Dialog(radix 非依存の自前モーダル)。
// 制御コンポーネント: open / onOpenChange を呼び出し側が保持する。
// Escape / オーバーレイクリックで閉じ、 表示中は body スクロールをロックする。
// 必ず document.body へ portal する。Card の backdrop-blur 等が作る
// containing block / stacking context に fixed オーバーレイが閉じ込められないようにする。

interface DialogProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly children: React.ReactNode;
}

function Dialog({ open, onOpenChange, children }: DialogProps): React.JSX.Element | null {
  // SSR / 初回 hydration では document が無いので、client mount 後にだけ portal する。
  const [portalTarget, setPortalTarget] = React.useState<HTMLElement | null>(
    null,
  );

  React.useEffect(() => {
    setPortalTarget(document.body);
  }, []);

  React.useEffect(() => {
    if (!open) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        onOpenChange(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, onOpenChange]);

  if (!open || portalTarget == null) {
    return null;
  }

  return createPortal(
    <div
      data-testid="dialog-overlay"
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
    >
      <div
        className="absolute inset-0 bg-black/80"
        aria-hidden="true"
        onClick={() => onOpenChange(false)}
      />
      <div
        role="dialog"
        aria-modal="true"
        className="relative z-10 w-full max-w-lg rounded-xl border border-white/10 bg-[#131313] p-6 shadow-xl"
      >
        {children}
      </div>
    </div>,
    portalTarget,
  );
}

function DialogHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): React.JSX.Element {
  return (
    <div
      className={cn("flex flex-col gap-1.5 pb-4", className)}
      {...props}
    />
  );
}

function DialogTitle({
  className,
  ...props
}: React.HTMLAttributes<HTMLHeadingElement>): React.JSX.Element {
  return (
    <h2
      className={cn("text-lg font-semibold text-slate-100", className)}
      {...props}
    />
  );
}

function DialogDescription({
  className,
  ...props
}: React.HTMLAttributes<HTMLParagraphElement>): React.JSX.Element {
  return (
    <p className={cn("text-sm text-slate-400", className)} {...props} />
  );
}

function DialogContent({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): React.JSX.Element {
  return <div className={cn("flex flex-col gap-3", className)} {...props} />;
}

function DialogFooter({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): React.JSX.Element {
  return (
    <div
      className={cn(
        "flex flex-col-reverse gap-2 pt-4 sm:flex-row sm:justify-end",
        className,
      )}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogContent,
  DialogFooter,
};
export type { DialogProps };
