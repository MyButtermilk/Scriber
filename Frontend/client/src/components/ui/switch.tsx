import * as React from "react"
import * as SwitchPrimitives from "@radix-ui/react-switch"

import { cn } from "@/lib/utils"

const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitives.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitives.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitives.Root
    className={cn("impact-echo-switch", className)}
    {...props}
    ref={ref}
  >
    <span className="impact-echo-switch__track-waves" aria-hidden="true">
      <span className="impact-echo-switch__wave impact-echo-switch__wave--off" />
      <span className="impact-echo-switch__wave impact-echo-switch__wave--on" />
    </span>

    <SwitchPrimitives.Thumb
      className="impact-echo-switch__thumb"
      aria-hidden="true"
    >
      <span className="impact-echo-switch__ambient-glow" />
      <span className="impact-echo-switch__marble-texture" />

      <span className="impact-echo-switch__icon-container">
        <svg viewBox="0 0 48 48" className="impact-echo-switch__icon impact-echo-switch__icon--x">
          <path className="line-1" d="M 9 9 L 39 39" />
          <path className="line-2" d="M 39 9 L 9 39" />
        </svg>
        <svg viewBox="0 0 48 48" className="impact-echo-switch__icon impact-echo-switch__icon--check">
          <path d="M 4 22 L 19 37 L 40 10" />
        </svg>
      </span>
    </SwitchPrimitives.Thumb>
  </SwitchPrimitives.Root>
))
Switch.displayName = SwitchPrimitives.Root.displayName

export { Switch }
