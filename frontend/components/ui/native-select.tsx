import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Chromium/Linux の native popup を dark 配色で描画する select。
 *
 * `color-scheme: dark` と option の明示色を併用する。見た目だけを共通化し、
 * native の keyboard/focus/disabled semantics はそのまま維持する。
 */
const NativeSelect = React.forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(({ className, children, ...props }, ref) => (
  <select
    ref={ref}
    className={cn(
      "native-dark-select flex h-9 w-full rounded-lg border border-white/15 px-3 py-1 text-sm shadow-sm transition-colors",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50",
      "disabled:cursor-not-allowed disabled:opacity-50",
      className,
    )}
    {...props}
  >
    {children}
  </select>
));
NativeSelect.displayName = "NativeSelect";

interface NativeSelectOptionProps
  extends React.OptionHTMLAttributes<HTMLOptionElement> {
  /** 選択可能だが未設定など、通常 option より弱く表示する状態。 */
  muted?: boolean;
}

const NativeSelectOption = React.forwardRef<
  HTMLOptionElement,
  NativeSelectOptionProps
>(({ className, muted = false, ...props }, ref) => (
  <option
    ref={ref}
    data-muted={muted ? "true" : undefined}
    className={cn("native-dark-select-option", className)}
    {...props}
  />
));
NativeSelectOption.displayName = "NativeSelectOption";

export { NativeSelect, NativeSelectOption };
