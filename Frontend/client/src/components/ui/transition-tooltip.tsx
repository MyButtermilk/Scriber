import { cloneElement, useId, type CSSProperties, type ReactElement } from "react";

import { cn } from "@/lib/utils";

interface TooltipTriggerProps {
  className?: string;
  "aria-label"?: string;
  "aria-describedby"?: string;
}

interface TransitionTooltipProps {
  content: string;
  children: ReactElement<TooltipTriggerProps>;
  className?: string;
  style?: CSSProperties;
}

export function TransitionTooltip({ content, children, className, style }: TransitionTooltipProps) {
  const tooltipId = useId();
  const alreadyNamedByTooltipText = children.props["aria-label"]?.trim() === content.trim();
  const describedBy = [children.props["aria-describedby"], alreadyNamedByTooltipText ? null : tooltipId]
    .filter(Boolean)
    .join(" ");
  const trigger = cloneElement(children, {
    className: cn(children.props.className, "t-tt-trigger"),
    "aria-describedby": describedBy || undefined,
  });

  return (
    <span className={cn("t-tt-wrap", className)} style={style}>
      {trigger}
      <span className="t-tt" id={tooltipId} role="tooltip" aria-hidden={alreadyNamedByTooltipText || undefined}>
        {content}
      </span>
    </span>
  );
}
