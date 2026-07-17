import type { MouseEvent } from "react";
import { Loader2, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

type DeleteActionButtonProps = {
  onClick: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  loading?: boolean;
  title?: string;
  label?: string;
  ariaLabel: string;
  size?: "md" | "sm";
  className?: string;
};

export function DeleteActionButton({
  onClick,
  disabled = false,
  loading = false,
  label = "Delete",
  ariaLabel,
  size = "md",
  className,
}: DeleteActionButtonProps) {
  const { t } = useI18n();
  return (
    <button
      type="button"
      className={cn("delete-pill", size === "sm" && "delete-pill--sm", className)}
      onClick={onClick}
      onKeyDown={(event) => event.stopPropagation()}
      disabled={disabled}
      data-label={t(label)}
      aria-label={ariaLabel}
    >
      {loading ? (
        <Loader2 className="delete-pill__spinner animate-spin" />
      ) : (
        <Trash2 className="delete-pill__icon" strokeWidth={2.1} />
      )}
    </button>
  );
}
