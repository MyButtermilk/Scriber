import type { MouseEvent } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

type CopyActionButtonProps = {
  onClick: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  copied?: boolean;
  title?: string;
  label?: string;
  copiedLabel?: string;
  ariaLabel: string;
  size?: "md" | "sm";
  className?: string;
};

export function CopyActionButton({
  onClick,
  disabled = false,
  copied = false,
  label = "Copy",
  copiedLabel = "Copied",
  ariaLabel,
  size = "md",
  className,
}: CopyActionButtonProps) {
  const hoverLabel = copied ? copiedLabel : label;

  return (
    <button
      type="button"
      className={cn("copy-pill", size === "sm" && "copy-pill--sm", copied && "is-copied", className)}
      onClick={onClick}
      disabled={disabled}
      data-label={hoverLabel}
      aria-label={ariaLabel}
    >
      {copied ? (
        <Check className="copy-pill__check" strokeWidth={2.4} />
      ) : (
        <Copy className="copy-pill__icon" strokeWidth={2.05} />
      )}
    </button>
  );
}
