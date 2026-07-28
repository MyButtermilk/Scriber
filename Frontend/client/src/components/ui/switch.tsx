import * as React from "react";
import * as SwitchPrimitives from "@radix-ui/react-switch";

import { cn } from "@/lib/utils";

const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitives.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitives.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitives.Root className={cn("impact-echo-switch", className)} {...props} ref={ref}>
    <SwitchPrimitives.Thumb className="impact-echo-switch__thumb" aria-hidden="true">
      <span className="impact-echo-switch__icon-container">
        <svg viewBox="0 0 48 48" className="impact-echo-switch__icon impact-echo-switch__icon--check">
          <path d="M 4 22 L 19 37 L 40 10" />
        </svg>
      </span>
    </SwitchPrimitives.Thumb>
  </SwitchPrimitives.Root>
));
Switch.displayName = SwitchPrimitives.Root.displayName;

export { Switch };
