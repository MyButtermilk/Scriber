import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface PageIntroProps {
  eyebrow: string;
  title: string;
  description: string;
  accentClassName?: string;
  titleAccessory?: ReactNode;
  actions?: ReactNode;
  bottomContent?: ReactNode;
  sticky?: boolean;
  className?: string;
}

export function PageIntro({
  eyebrow,
  title,
  description,
  accentClassName = "bg-primary/60",
  titleAccessory,
  actions,
  bottomContent,
  sticky = true,
  className,
}: PageIntroProps) {
  // Kept for source compatibility. The restrained header no longer renders a decorative accent rule.
  void accentClassName;

  return (
    <header
      data-page-intro="true"
      data-sticky={sticky ? "true" : "false"}
      data-has-actions={actions ? "true" : "false"}
      data-has-bottom-content={bottomContent ? "true" : "false"}
      className={cn(
        "page-intro transcription-intro mb-5 border-b border-border/65 px-1 pb-4 text-left md:mb-6 md:pb-5",
        sticky ? "sticky top-3 z-20" : "relative z-0",
        bottomContent && "pb-0 md:pb-0",
        className,
      )}
    >
      <div className="page-intro__grid grid min-w-0 gap-3">
        <div className="page-intro__heading min-w-0">
          <p className="page-intro__context mb-2 text-xs font-medium leading-4 text-muted-foreground">{eyebrow}</p>

          <div className="page-intro__title-row flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
            <h1 className="page-intro__title min-w-0 text-balance font-heading text-[30px] font-bold leading-[1.05] tracking-[-0.035em] text-foreground [overflow-wrap:anywhere] md:text-[36px]">
              {title}
            </h1>
            {titleAccessory ? <div className="page-intro__title-accessory shrink-0">{titleAccessory}</div> : null}
          </div>
        </div>

        <p className="page-intro__description max-w-[65ch] text-pretty text-sm leading-6 text-muted-foreground">
          {description}
        </p>

        {actions ? <div className="page-intro__actions flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>

      {bottomContent ? (
        <div className="page-intro__bottom mt-4 border-t border-border/55 py-2.5">{bottomContent}</div>
      ) : null}
    </header>
  );
}
