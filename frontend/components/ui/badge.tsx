import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

// shadcn/ui Badge プリミティブ(cva variant ベース)。
// Synthetix Vibe: Electric Purple / Pulse Red / slate のステータス配色。
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium",
  {
    variants: {
      variant: {
        default: "border-white/10 bg-white/5 text-slate-300",
        active: "border-primary/40 bg-primary/15 text-primary",
        danger: "border-danger/40 bg-danger/15 text-danger",
        muted: "border-white/10 bg-transparent text-slate-400",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps): React.JSX.Element {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
