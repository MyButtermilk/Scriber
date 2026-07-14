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
        className,
      )}
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : "Scriber"}
      role={decorative ? undefined : "img"}
    >
      <img
        src="/favicon.svg"
        alt=""
        className="h-[70%] w-[70%] object-contain dark:hidden"
        draggable={false}
      />
      <img
        src="/favicon-dark.svg"
        alt=""
        className="hidden h-full w-full object-contain dark:block"
        draggable={false}
      />
    </span>
  );
}
