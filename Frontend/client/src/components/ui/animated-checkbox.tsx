import type { CSSProperties, MouseEventHandler } from "react";

import { cn } from "@/lib/utils";

interface AnimatedCheckboxProps {
  checked: boolean;
  ariaLabel: string;
  disabled?: boolean;
  onClick: MouseEventHandler<HTMLButtonElement>;
  className?: string;
}

export function AnimatedCheckbox({ checked, ariaLabel, disabled = false, onClick, className }: AnimatedCheckboxProps) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={onClick}
      className={cn("t-check", className)}
      style={{ "--check-len": 15 } as CSSProperties}
    >
      <svg viewBox="0 0 10.1668 10.1668" fill="none" aria-hidden="true">
        <path
          d="M1 5.52L3.92 9.17L9.17 1"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}
