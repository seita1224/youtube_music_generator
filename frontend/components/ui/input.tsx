import * as React from "react";

import { cn } from "@/lib/utils";

// shadcn/ui 風 Input(radix 非依存の素の <input>)。
// dark: 白半透明ボーダー + 透過背景。 focus は primary リング。
const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, type = "text", ...props }, ref) => (
  <input
    ref={ref}
    type={type}
    className={cn(
      "flex h-9 w-full rounded-lg border border-white/15 bg-white/5 px-3 py-1 text-sm text-slate-100 shadow-sm transition-colors",
      "placeholder:text-slate-500",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50",
      "disabled:cursor-not-allowed disabled:opacity-50",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";

export { Input };
