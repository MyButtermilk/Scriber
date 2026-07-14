import * as React from "react"
import * as ToastPrimitives from "@radix-ui/react-toast"
import { cva, type VariantProps } from "class-variance-authority"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"

const ToastProvider = ToastPrimitives.Provider

const ToastViewport = React.forwardRef<
  React.ElementRef<typeof ToastPrimitives.Viewport>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitives.Viewport>
>(({ className, ...props }, ref) => (
  <ToastPrimitives.Viewport
    ref={ref}
    className={cn(
      "fixed top-0 z-[100] flex max-h-screen w-full flex-col-reverse p-4 sm:bottom-0 sm:right-0 sm:top-auto sm:flex-col md:max-w-[420px]",
      className
    )}
    {...props}
  />
))
ToastViewport.displayName = ToastPrimitives.Viewport.displayName

const toastVariants = cva(
  "group pointer-events-auto relative flex w-full items-center justify-between gap-4 overflow-hidden rounded-xl border p-4 pr-9 shadow-[0_18px_45px_rgba(15,23,42,0.14)] backdrop-blur-sm transition-transform duration-[var(--duration-medium)] ease-[var(--ease-smooth-out)] data-[swipe=cancel]:translate-x-0 data-[swipe=end]:translate-x-[var(--radix-toast-swipe-end-x)] data-[swipe=move]:translate-x-[var(--radix-toast-swipe-move-x)] data-[swipe=move]:transition-none data-[state=open]:animate-in data-[state=open]:duration-[var(--duration-slow)] data-[state=open]:slide-in-from-top-full data-[state=open]:sm:slide-in-from-bottom-full data-[state=closed]:animate-out data-[state=closed]:duration-[var(--duration-medium)] data-[state=closed]:fade-out-80 data-[state=closed]:slide-out-to-right-full data-[swipe=end]:animate-out motion-reduce:[--tw-enter-translate-x:0] motion-reduce:[--tw-enter-translate-y:0] motion-reduce:[--tw-exit-translate-x:0] motion-reduce:[--tw-exit-translate-y:0]",
  {
    variants: {
      variant: {
        default: "border-slate-200/80 bg-white/95 text-slate-950 dark:border-slate-800/80 dark:bg-slate-950/95 dark:text-slate-50",
        destructive:
          "destructive group border-red-500 bg-red-600 text-white shadow-[0_18px_45px_rgba(185,28,28,0.24)]",
        update:
          "update group cursor-pointer border-blue-200/80 bg-white/95 text-slate-950 shadow-[0_22px_55px_rgba(37,99,235,0.18)] hover:border-blue-300 hover:bg-blue-50/95 dark:border-blue-500/30 dark:bg-slate-950/95 dark:text-slate-50 dark:hover:border-blue-400/60 dark:hover:bg-slate-900/95",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

const Toast = React.forwardRef<
  React.ElementRef<typeof ToastPrimitives.Root>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitives.Root> &
    VariantProps<typeof toastVariants>
>(({ className, variant, ...props }, ref) => {
  return (
    <ToastPrimitives.Root
      ref={ref}
      className={cn(toastVariants({ variant }), className)}
      {...props}
    />
  )
})
Toast.displayName = ToastPrimitives.Root.displayName

const ToastAction = React.forwardRef<
  React.ElementRef<typeof ToastPrimitives.Action>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitives.Action>
>(({ className, ...props }, ref) => (
  <ToastPrimitives.Action
    ref={ref}
    className={cn(
      "inline-flex h-8 shrink-0 items-center justify-center rounded-lg border bg-transparent px-3 text-sm font-semibold ring-offset-background transition-colors duration-[var(--duration-quick)] ease-[var(--ease-smooth-out)] hover:bg-secondary focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 active:translate-y-px disabled:pointer-events-none disabled:opacity-50 group-[.destructive]:border-white/30 group-[.destructive]:hover:bg-white/10 group-[.destructive]:focus:ring-white/40 group-[.update]:border-blue-600 group-[.update]:bg-blue-600 group-[.update]:text-white group-[.update]:shadow-[0_10px_22px_rgba(37,99,235,0.22)] group-[.update]:hover:bg-blue-700 group-[.update]:focus:ring-blue-500 motion-reduce:transition-none motion-reduce:active:translate-y-0",
      className
    )}
    {...props}
  />
))
ToastAction.displayName = ToastPrimitives.Action.displayName

const ToastClose = React.forwardRef<
  React.ElementRef<typeof ToastPrimitives.Close>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitives.Close>
>(({ className, ...props }, ref) => (
  <ToastPrimitives.Close
    ref={ref}
    className={cn(
      "absolute right-2 top-2 rounded-md p-1 text-foreground/50 opacity-0 transition-opacity duration-[var(--duration-quick)] ease-[var(--ease-smooth-out)] hover:text-foreground focus:opacity-100 focus:outline-none focus:ring-2 group-hover:opacity-100 group-[.destructive]:text-red-100 group-[.destructive]:hover:text-white group-[.destructive]:focus:ring-white/40 group-[.update]:text-blue-700/70 group-[.update]:hover:text-blue-950 group-[.update]:focus:ring-blue-500 dark:group-[.update]:text-blue-200/70 dark:group-[.update]:hover:text-blue-50 motion-reduce:transition-none",
      className
    )}
    toast-close=""
    {...props}
  >
    <X className="h-4 w-4" />
  </ToastPrimitives.Close>
))
ToastClose.displayName = ToastPrimitives.Close.displayName

const ToastTitle = React.forwardRef<
  React.ElementRef<typeof ToastPrimitives.Title>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitives.Title>
>(({ className, ...props }, ref) => (
  <ToastPrimitives.Title
    ref={ref}
    className={cn("text-sm font-bold tracking-tight", className)}
    {...props}
  />
))
ToastTitle.displayName = ToastPrimitives.Title.displayName

const ToastDescription = React.forwardRef<
  React.ElementRef<typeof ToastPrimitives.Description>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitives.Description>
>(({ className, ...props }, ref) => (
  <ToastPrimitives.Description
    ref={ref}
    className={cn("text-sm leading-5 text-slate-600 dark:text-slate-300 group-[.destructive]:text-white/90", className)}
    {...props}
  />
))
ToastDescription.displayName = ToastPrimitives.Description.displayName

type ToastProps = React.ComponentPropsWithoutRef<typeof Toast>

type ToastActionElement = React.ReactElement<typeof ToastAction>

export {
  type ToastProps,
  type ToastActionElement,
  ToastProvider,
  ToastViewport,
  Toast,
  ToastTitle,
  ToastDescription,
  ToastClose,
  ToastAction,
}
