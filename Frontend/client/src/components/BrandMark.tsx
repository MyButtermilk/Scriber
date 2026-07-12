import { cn } from "@/lib/utils";

interface BrandMarkProps {
  className?: string;
  decorative?: boolean;
}

export function BrandMark({ className, decorative = false }: BrandMarkProps) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-[10px] border border-foreground/[0.09]",
        "bg-background/55 shadow-[inset_0_1px_0_hsl(var(--foreground)/0.06),0_1px_2px_hsl(var(--foreground)/0.08)]",
        "dark:border-white/[0.12] dark:bg-white/[0.07] dark:shadow-[inset_0_1px_0_rgb(255_255_255/0.08),0_1px_3px_rgb(0_0_0/0.28)]",
        className,
      )}
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : "Scriber"}
      role={decorative ? undefined : "img"}
    >
      <img src="/favicon.svg" alt="" className="h-[70%] w-[70%] object-contain dark:hidden" draggable={false} />
      <img src="/favicon-dark.svg" alt="" className="hidden h-[70%] w-[70%] object-contain dark:block" draggable={false} />
    </span>
  );
}
