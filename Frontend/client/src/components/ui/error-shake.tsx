import { useLayoutEffect, useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";

interface ErrorShakeProps {
  active: boolean;
  trigger: unknown;
  children: ReactNode;
  className?: string;
}

function motionDuration(name: string, fallback: number): number {
  const value = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
  return Number.isFinite(value) ? value : fallback;
}

export function ErrorShake({ active, trigger, children, className }: ErrorShakeProps) {
  const inputRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    if (!active) {
      input.classList.remove("is-shaking");
      return;
    }

    input.classList.remove("is-shaking");
    void input.offsetWidth;
    input.classList.add("is-shaking");
    const shakeDuration = motionDuration("--shake-dur-a", 80) * 2 + motionDuration("--shake-dur-b", 60) * 2;
    const timer = window.setTimeout(() => input.classList.remove("is-shaking"), shakeDuration + 20);
    return () => {
      window.clearTimeout(timer);
      input.classList.remove("is-shaking");
    };
  }, [active, trigger]);

  return (
    <div className={cn("t-input-wrap", active && "is-error", className)}>
      <div ref={inputRef} className={cn("t-input", active && "is-error")}>
        {children}
      </div>
    </div>
  );
}
