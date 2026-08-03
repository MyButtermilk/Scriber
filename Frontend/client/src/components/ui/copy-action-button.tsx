import type { MouseEvent } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";
import { TransitionIcon } from "@/components/ui/transition-state";

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
  const { t } = useI18n();
  const hoverLabel = t(copied ? copiedLabel : label);

  return (
    <button
      type="button"
      className={cn("copy-pill", size === "sm" && "copy-pill--sm", copied && "is-copied", className)}
      onClick={onClick}
      onKeyDown={(event) => event.stopPropagation()}
      disabled={disabled}
      data-label={hoverLabel}
      aria-label={ariaLabel}
    >
      <TransitionIcon
        state={copied ? "b" : "a"}
        iconA={<Copy className="copy-pill__icon" strokeWidth={2.05} />}
        iconB={<Check className="copy-pill__check" strokeWidth={2.4} />}
      />
    </button>
  );
}
