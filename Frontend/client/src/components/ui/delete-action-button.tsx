import type { MouseEvent } from "react";
import { Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";
import { WavePhysicsLoader } from "@/components/ui/wave-physics-loader";

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
  title,
  label = "Delete",
  ariaLabel,
  size = "md",
  className,
}: DeleteActionButtonProps) {
  const { t } = useI18n();
  return (
    <button
      type="button"
      className={cn("delete-pill ui-pressable ui-hit-target", size === "sm" && "delete-pill--sm", className)}
      onClick={onClick}
      onKeyDown={(event) => event.stopPropagation()}
      disabled={disabled}
      data-label={t(label)}
      title={title}
      aria-label={ariaLabel}
      aria-busy={loading || undefined}
    >
      {loading ? <WavePhysicsLoader size="micro" /> : <Trash2 className="delete-pill__icon" strokeWidth={2.1} />}
    </button>
  );
}
