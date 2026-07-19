import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-black tracking-wider uppercase whitespace-nowrap transition-colors",
  {
    variants: {
      variant: {
        default: "bg-foreground/10 text-foreground",
        primary: "bg-primary/10 text-primary border border-primary/20",
        secondary: "bg-secondary text-secondary-foreground",
        destructive: "bg-destructive/10 text-destructive border border-destructive/20",
        outline: "border border-border text-foreground",
        muted: "bg-foreground/5 text-muted-foreground",
        running: "bg-blue-500/10 text-blue-700 border border-blue-500/20",
        completed: "bg-emerald-500/10 text-emerald-700 border border-emerald-500/20",
        failed: "bg-red-500/10 text-red-700 border border-red-500/20",
        // trace 稳定性重构新增（设计 20260720_203000）：
        //   interrupted = 橙色（警示色，提示用户需手动收敛）
        //   cancelled = 灰色（中性色，用户主动放弃）
        interrupted: "bg-orange-500/10 text-orange-700 border border-orange-500/20",
        cancelled: "bg-foreground/10 text-muted-foreground border border-border",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
