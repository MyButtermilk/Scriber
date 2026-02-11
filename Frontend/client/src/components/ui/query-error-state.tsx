import { AlertCircle, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { cn } from "@/lib/utils";

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
  return (
    <Alert variant="destructive" className={cn("relative", className)}>
      <AlertCircle className="h-4 w-4" />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="pr-20">
        {description}
      </AlertDescription>
      {onRetry && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="absolute right-3 top-1/2 -translate-y-1/2"
          onClick={onRetry}
        >
          <RotateCcw className="mr-1 h-3.5 w-3.5" />
          Retry
        </Button>
      )}
    </Alert>
  );
}

