import { useEffect } from "react";
import { useLocation } from "wouter";

import { useI18n } from "@/i18n";
import { routeTitleSource } from "@/lib/route-titles";

export function RouteDocumentTitle() {
  const [location] = useLocation();
  const { t } = useI18n();

  useEffect(() => {
    document.title = `${t(routeTitleSource(location))} | Scriber`;
  }, [location, t]);

  return null;
}
