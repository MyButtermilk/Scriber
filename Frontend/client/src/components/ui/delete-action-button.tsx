import type { KeyboardEvent, MouseEvent } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";
import { WavePhysicsLoader } from "@/components/ui/wave-physics-loader";

/** How long an armed delete waits for its confirming second click. */
export const DELETE_CONFIRM_TIMEOUT_MS = 3500;

type DeleteActionButtonProps = {
  onClick: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  loading?: boolean;
  title?: string;
  label?: string;
  ariaLabel: string;
  size?: "md" | "sm";
  className?: string;
  /**
   * Require an inline second click before `onClick` runs. The first click
   * only arms the button; Escape, blur, or the timeout disarm it again.
   */
  requireConfirm?: boolean;
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
  requireConfirm = true,
}: DeleteActionButtonProps) {
  const { t } = useI18n();
  const [confirming, setConfirming] = useState(false);
  const resetTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const clearResetTimer = useCallback(() => {
    if (resetTimerRef.current !== undefined) {
      clearTimeout(resetTimerRef.current);
      resetTimerRef.current = undefined;
    }
  }, []);

  const disarm = useCallback(() => {
    clearResetTimer();
    setConfirming(false);
  }, [clearResetTimer]);

  // A busy, disabled, or no-longer-confirming button must never stay armed.
  useEffect(() => {
    if (disabled || loading || !requireConfirm) disarm();
  }, [disabled, loading, requireConfirm, disarm]);

  useEffect(() => clearResetTimer, [clearResetTimer]);

  const handleClick = useCallback(
    (event: MouseEvent<HTMLButtonElement>) => {
      if (disabled || loading) return;
      if (requireConfirm && !confirming) {
        // Arm only: keep the click from reaching row/card navigation handlers.
        event.preventDefault();
        event.stopPropagation();
        clearResetTimer();
        setConfirming(true);
        resetTimerRef.current = setTimeout(() => {
          resetTimerRef.current = undefined;
          setConfirming(false);
        }, DELETE_CONFIRM_TIMEOUT_MS);
        return;
      }
      disarm();
      onClick(event);
    },
    [clearResetTimer, confirming, disabled, disarm, loading, onClick, requireConfirm],
  );

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      if (event.key === "Escape" && confirming) {
        event.preventDefault();
        disarm();
      }
    },
    [confirming, disarm],
  );

  const isConfirming = confirming && !disabled && !loading;

  return (
    <button
      type="button"
      className={cn(
        "delete-pill ui-pressable ui-hit-target",
        size === "sm" && "delete-pill--sm",
        isConfirming && "is-confirming",
        className,
      )}
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      onBlur={confirming ? disarm : undefined}
      disabled={disabled}
      data-label={isConfirming ? t("Confirm?") : t(label)}
      data-confirming={isConfirming ? "true" : undefined}
      title={isConfirming ? t("Confirm deletion") : title}
      aria-label={isConfirming ? `${t("Confirm deletion")}: ${ariaLabel}` : ariaLabel}
      aria-busy={loading || undefined}
    >
      {loading ? <WavePhysicsLoader size="micro" /> : <Trash2 className="delete-pill__icon" strokeWidth={2.1} />}
    </button>
  );
}
