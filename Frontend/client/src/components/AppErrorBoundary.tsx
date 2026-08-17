import { Component, useEffect, useRef, type ErrorInfo, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type AppErrorBoundaryVariant = "root" | "route";

interface AppErrorBoundaryProps {
  children: ReactNode;
  onNavigateHome?: () => void;
  resetKey?: string;
  variant: AppErrorBoundaryVariant;
}

interface AppErrorBoundaryState {
  hasError: boolean;
}

const INITIAL_STATE: AppErrorBoundaryState = {
  hasError: false,
};

function AppErrorFallback({
  onNavigateHome,
  onRetry,
  variant,
}: {
  onNavigateHome?: () => void;
  onRetry: () => void;
  variant: AppErrorBoundaryVariant;
}) {
  const { t } = useI18n();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const isRootFallback = variant === "root";
  const headingId = `scriber-${variant}-error-heading`;
  const descriptionId = `scriber-${variant}-error-description`;

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <div
      className={
        isRootFallback
          ? "flex min-h-[100dvh] items-center justify-center bg-background px-5 py-10 text-foreground antialiased"
          : "flex min-h-[320px] items-center justify-center px-5 py-10 text-foreground antialiased"
      }
      role="alert"
      aria-labelledby={headingId}
      aria-describedby={descriptionId}
    >
      <div className="w-full max-w-lg rounded-xl border border-border bg-card p-6 shadow-sm">
        <h1
          ref={headingRef}
          id={headingId}
          tabIndex={-1}
          className="text-balance font-heading text-xl font-semibold tracking-tight outline-none"
        >
          {t(isRootFallback ? "Scriber could not start" : "This section could not be opened")}
        </h1>
        <p id={descriptionId} className="mt-2 text-pretty text-sm leading-6 text-muted-foreground">
          {t(
            isRootFallback
              ? "Reload Scriber to try again."
              : "The rest of Scriber is still available. Try opening this section again.",
          )}
        </p>
        <div className="mt-5 flex flex-wrap gap-2">
          <Button type="button" onClick={onRetry} className="min-h-10">
            {t(isRootFallback ? "Reload Scriber" : "Try again")}
          </Button>
          {!isRootFallback && onNavigateHome && (
            <Button type="button" variant="outline" onClick={onNavigateHome} className="min-h-10">
              {t("Back to Live Mic")}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = INITIAL_STATE;

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`Scriber ${this.props.variant} render failed.`, error, info.componentStack);
  }

  componentDidUpdate(previousProps: AppErrorBoundaryProps) {
    if (this.state.hasError && previousProps.resetKey !== this.props.resetKey) {
      this.setState(INITIAL_STATE);
    }
  }

  private handleRetry = () => {
    if (this.props.variant === "root") {
      window.location.reload();
      return;
    }
    this.setState(INITIAL_STATE);
  };

  render() {
    if (this.state.hasError) {
      return (
        <AppErrorFallback
          variant={this.props.variant}
          onRetry={this.handleRetry}
          onNavigateHome={this.props.onNavigateHome}
        />
      );
    }

    return this.props.children;
  }
}
