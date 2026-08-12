import { AlertCircle, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

interface QueryErrorStateProps {
  title?: string;
  description?: string;
  onRetry?: () => void;
  className?: string;
}

export function QueryErrorState({
  title = "Could not load data",
  description = "Please check your connection and try again.",
  onRetry,
  className,
}: QueryErrorStateProps) {
  const { t } = useI18n();
  return (
    <Alert variant="destructive" className={cn("query-error-state", className)}>
      <AlertCircle className="h-4 w-4" />
      <div className="query-error-state__copy min-w-0">
        <AlertTitle>{t(title)}</AlertTitle>
        <AlertDescription>{t(description)}</AlertDescription>
      </div>
      {onRetry && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="query-error-state__action w-fit max-w-full"
          onClick={onRetry}
        >
          <RotateCcw className="mr-1 h-3.5 w-3.5" />
          {t("Retry")}
        </Button>
      )}
    </Alert>
  );
}
