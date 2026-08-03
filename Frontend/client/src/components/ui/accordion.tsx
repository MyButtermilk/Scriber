import * as React from "react";
import * as AccordionPrimitive from "@radix-ui/react-accordion";
import { ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

const Accordion = AccordionPrimitive.Root;

const AccordionItem = React.forwardRef<
  React.ElementRef<typeof AccordionPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof AccordionPrimitive.Item>
>(({ className, ...props }, ref) => (
  <AccordionPrimitive.Item ref={ref} className={cn("t-acc border-b", className)} {...props} />
));
AccordionItem.displayName = "AccordionItem";

const AccordionTrigger = React.forwardRef<
  React.ElementRef<typeof AccordionPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof AccordionPrimitive.Trigger>
>(({ className, children, ...props }, ref) => (
  <AccordionPrimitive.Header className="flex">
    <AccordionPrimitive.Trigger
      ref={ref}
      className={cn(
        "flex flex-1 items-center justify-between py-4 text-left text-sm font-medium hover:underline",
        className,
      )}
      {...props}
    >
      {children}
      <span className="t-acc-chevron shrink-0">
        <ChevronDown className="h-4 w-4 text-muted-foreground" />
      </span>
    </AccordionPrimitive.Trigger>
  </AccordionPrimitive.Header>
));
AccordionTrigger.displayName = AccordionPrimitive.Trigger.displayName;

interface AccordionPanelProps extends React.HTMLAttributes<HTMLDivElement> {
  innerClassName?: string;
  "data-state"?: "open" | "closed";
}

const AccordionPanel = React.forwardRef<HTMLDivElement, AccordionPanelProps>(
  ({ children, innerClassName, "data-state": state, ...props }, ref) => {
    const closed = state === "closed";
    return (
      <div ref={ref} {...props} data-state={state} aria-hidden={closed || undefined} inert={closed}>
        <div className="t-acc-panel" aria-hidden={closed || undefined} inert={closed}>
          <div className={cn("t-acc-panel-inner pb-4 pt-0", innerClassName)}>{children}</div>
        </div>
      </div>
    );
  },
);
AccordionPanel.displayName = "AccordionPanel";

const AccordionContent = React.forwardRef<
  React.ElementRef<typeof AccordionPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof AccordionPrimitive.Content>
>(({ className, children, ...props }, ref) => (
  <AccordionPrimitive.Content ref={ref} className="t-acc-presence text-sm" forceMount asChild {...props}>
    <AccordionPanel innerClassName={className}>{children}</AccordionPanel>
  </AccordionPrimitive.Content>
));
AccordionContent.displayName = AccordionPrimitive.Content.displayName;

export { Accordion, AccordionItem, AccordionTrigger, AccordionContent };
