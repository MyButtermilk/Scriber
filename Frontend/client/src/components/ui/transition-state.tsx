import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

interface TransitionTextProps {
  value: string;
  className?: string;
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}

export function TransitionText({ value, className }: TransitionTextProps) {
  const [displayedValue, setDisplayedValue] = useState(value);
  const elementRef = useRef<HTMLSpanElement>(null);
  const pendingEntryRef = useRef(false);

  useEffect(() => {
    const element = elementRef.current;
    if (value === displayedValue) {
      pendingEntryRef.current = false;
      element?.classList.remove("is-exit", "is-enter-start");
      return;
    }
    if (prefersReducedMotion()) {
      pendingEntryRef.current = false;
      element?.classList.remove("is-exit", "is-enter-start");
      setDisplayedValue(value);
      return;
    }

    if (!element) {
      setDisplayedValue(value);
      return;
    }

    const rawDuration = getComputedStyle(document.documentElement).getPropertyValue("--text-swap-dur");
    const duration = Number.parseFloat(rawDuration) || 150;
    element.classList.add("is-exit");
    const timer = window.setTimeout(() => {
      pendingEntryRef.current = true;
      setDisplayedValue(value);
    }, duration);

    return () => window.clearTimeout(timer);
  }, [displayedValue, value]);

  useLayoutEffect(() => {
    if (!pendingEntryRef.current) return;
    const element = elementRef.current;
    if (!element) return;

    element.classList.remove("is-exit");
    element.classList.add("is-enter-start");
    void element.offsetHeight;
    element.classList.remove("is-enter-start");
    pendingEntryRef.current = false;
  }, [displayedValue]);

  return (
    <span ref={elementRef} className={cn("t-text-swap", className)} aria-label={value}>
      {displayedValue}
    </span>
  );
}

interface TransitionIconProps {
  state: "a" | "b";
  iconA: ReactNode;
  iconB: ReactNode;
  className?: string;
}

export function TransitionIcon({ state, iconA, iconB, className }: TransitionIconProps) {
  return (
    <span className={cn("t-icon-swap", className)} data-state={state} aria-hidden="true">
      <span className="t-icon" data-icon="a">
        {iconA}
      </span>
      <span className="t-icon" data-icon="b">
        {iconB}
      </span>
    </span>
  );
}

interface SuccessCheckIconProps {
  visible?: boolean;
  className?: string;
  animateOnMount?: boolean;
}

export function SuccessCheckIcon({ visible = true, className, animateOnMount = true }: SuccessCheckIconProps) {
  const previousVisibleRef = useRef(visible);
  const [visualState, setVisualState] = useState<"in" | "out" | "steady">(() =>
    visible ? (animateOnMount ? "in" : "steady") : "out",
  );

  useLayoutEffect(() => {
    const wasVisible = previousVisibleRef.current;
    previousVisibleRef.current = visible;
    if (!visible) {
      setVisualState("out");
    } else if (!wasVisible) {
      setVisualState("in");
    }
  }, [visible]);

  return (
    <span className="t-success-check" data-state={visualState} aria-hidden="true">
      <svg className={className} viewBox="0 0 24 24" fill="none">
        <path
          d="M5 12.5L9.2 16.5L19 7"
          stroke="currentColor"
          strokeWidth="2.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}
