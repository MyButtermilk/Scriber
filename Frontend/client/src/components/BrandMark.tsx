import { cn } from "@/lib/utils";

interface BrandMarkProps {
  className?: string;
  decorative?: boolean;
}

export function BrandMark({ className, decorative = false }: BrandMarkProps) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center",
        "dark:rounded-full dark:bg-white dark:ring-1 dark:ring-black/10 dark:shadow-[0_1px_3px_rgb(0_0_0/0.28)]",
        className,
      )}
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : "Scriber"}
      role={decorative ? undefined : "img"}
    >
      <img src="/favicon.svg" alt="" className="h-[70%] w-[70%] object-contain" draggable={false} />
    </span>
  );
}
