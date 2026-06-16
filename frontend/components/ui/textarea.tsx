import * as React from "react";

import { cn } from "@/lib/utils";

// shadcn/ui 風 Textarea(radix 非依存の素の <textarea>)。
// dryrun 却下理由入力に使用(min 4 文字バリデーションは呼び出し側)。
const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      "flex min-h-20 w-full rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-sm text-slate-100 shadow-sm transition-colors",
      "placeholder:text-slate-500",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50",
      "disabled:cursor-not-allowed disabled:opacity-50",
      className,
    )}
    {...props}
  />
));
Textarea.displayName = "Textarea";

export { Textarea };
