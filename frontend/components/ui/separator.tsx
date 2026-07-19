import * as React from "react";

import { cn } from "@/lib/utils";

interface SeparatorProps extends React.HTMLAttributes<HTMLDivElement> {
  readonly orientation?: "horizontal" | "vertical";
}

// shadcn/ui Separator(radix 非依存の素実装)。 サイドバーの区切りに使用。
function Separator({
  className,
  orientation = "horizontal",
  ...props
}: SeparatorProps): React.JSX.Element {
  return (
    <div
      role="separator"
      aria-orientation={orientation}
      className={cn(
        "shrink-0 bg-white/10",
        orientation === "horizontal" ? "h-px w-full" : "h-full w-px",
        className,
      )}
      {...props}
    />
  );
}

export { Separator };
