import { Link } from "wouter";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

export default function NotFound() {
  const { t } = useI18n();
  return (
    <div
      className="app-page-shell flex min-h-full items-start px-4 py-12 md:px-6 md:py-20"
      data-page-shell="not-found"
      data-page-layout="recovery"
    >
      <section className="w-full max-w-2xl border-t border-border pt-7" aria-labelledby="not-found-title">
        <p className="font-mono text-xs font-medium tabular-nums text-muted-foreground" aria-hidden="true">
          404
        </p>
        <h1
          id="not-found-title"
          className="mt-3 font-heading text-3xl font-bold leading-tight tracking-[-0.035em] text-foreground md:text-4xl"
        >
          {t("Page not found")}
        </h1>
        <p className="mt-4 max-w-[55ch] text-base leading-7 text-muted-foreground">
          {t("The page you requested does not exist or may have moved.")}
        </p>
        <Button asChild className="mt-7 whitespace-nowrap">
          <Link href="/">{t("Back to Live Mic")}</Link>
        </Button>
      </section>
    </div>
  );
}
