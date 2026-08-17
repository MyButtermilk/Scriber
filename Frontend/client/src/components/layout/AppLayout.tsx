import { Link, useLocation } from "wouter";
import {
  CalendarClock,
  Mic,
  Settings,
  Youtube,
  FolderOpen,
  Menu,
  Search,
  Terminal,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { SidebarSearch } from "@/components/ui/sidebar-search";
import { ThemeToggle } from "@/components/theme-toggle";
import { DesktopTitleBar } from "@/components/DesktopTitleBar";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { preloadRouteChunk } from "@/lib/route-preload";
import { BrandMark } from "@/components/BrandMark";
import { ActiveMeetingPill } from "@/components/meeting/ActiveMeetingPill";
import { LanguageToggle } from "@/components/language-toggle";
import { useI18n } from "@/i18n";
import { AppScrollContainerContext } from "@/contexts/AppScrollContainerContext";
import { AppOverlayScrollbar } from "@/components/layout/AppOverlayScrollbar";

const CommandPalette = lazy(async () => {
  const module = await import("@/components/CommandPalette");
  return { default: module.CommandPalette };
});

interface AppLayoutProps {
  children: React.ReactNode;
  path?: string;
}

type NavigationItem = {
  href: string;
  icon: LucideIcon;
  label: string;
};

export function AppLayout({ children, path }: AppLayoutProps) {
  const [location] = useLocation();
  const { t } = useI18n();
  const currentKey = path || location;
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const [commandOpen, setCommandOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  // Global Strg+K handler for Command Palette
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        void import("@/components/CommandPalette");
        setCommandOpen((open) => !open);
      }
    };
    document.addEventListener("keydown", down);
    return () => document.removeEventListener("keydown", down);
  }, []);

  // Callback for SidebarSearch to open Command Palette
  const handleOpenCommandPalette = useCallback(() => {
    void import("@/components/CommandPalette");
    setCommandOpen(true);
  }, []);

  const handleOpenCommandPaletteFromSheet = useCallback(() => {
    setMobileNavOpen(false);
    void import("@/components/CommandPalette");
    setCommandOpen(true);
  }, []);

  // Preload route chunks on intent to keep navigation responsive.
  const handleNavIntent = useCallback((href: string) => {
    void preloadRouteChunk(href);
  }, []);

  useEffect(() => {
    scrollContainerRef.current?.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [currentKey]);

  const primaryTabs: NavigationItem[] = [
    { href: "/", icon: Mic, label: t("Live Mic") },
    { href: "/meetings", icon: CalendarClock, label: t("Meetings") },
    { href: "/youtube", icon: Youtube, label: t("YouTube") },
    { href: "/file", icon: FolderOpen, label: t("File") },
  ];

  const utilityTabs: NavigationItem[] = [{ href: "/settings", icon: Settings, label: t("Settings") }];

  const navigationItemClassName = (isActive: boolean) =>
    cn(
      "relative flex min-h-10 min-w-0 cursor-pointer items-center gap-3 rounded-[10px] border",
      "border-transparent px-3 text-ui-body-sm font-medium no-underline outline-none",
      "transition-[background-color,border-color,color,transform] duration-[var(--duration-quick)]",
      "ease-[var(--ease-smooth-out)] hover:bg-foreground/5 hover:text-foreground active:scale-[0.985]",
      "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-sidebar",
      "motion-reduce:transition-none motion-reduce:active:scale-100",
      isActive ? "border-primary/20 bg-primary/10 text-foreground dark:bg-primary/15" : "text-muted-foreground",
    );

  const renderActiveIndicator = (isActive: boolean) => (
    <span
      className={cn(
        "absolute bottom-2.5 left-1 top-2.5 w-[3px] origin-center rounded-full bg-primary",
        "transition-[opacity,transform] duration-[var(--duration-quick)] ease-[var(--ease-smooth-out)]",
        "motion-reduce:transition-none",
        isActive ? "scale-y-100 opacity-100" : "scale-y-50 opacity-0",
      )}
      aria-hidden="true"
    />
  );

  const renderTabItem = (tab: NavigationItem, onNavigate?: () => void) => {
    const isActive = location === tab.href || (tab.href !== "/" && location.startsWith(tab.href));
    const Icon = tab.icon;

    return (
      <li key={tab.href}>
        <Link
          href={tab.href}
          onPointerEnter={() => handleNavIntent(tab.href)}
          onPointerDown={() => handleNavIntent(tab.href)}
          onFocus={() => handleNavIntent(tab.href)}
          onClick={onNavigate}
          aria-current={isActive ? "page" : undefined}
          data-active={isActive ? "true" : "false"}
          className={navigationItemClassName(isActive)}
        >
          {renderActiveIndicator(isActive)}
          <Icon
            className={cn(
              "h-[18px] w-[18px] shrink-0 stroke-[1.75px] transition-colors",
              isActive && "stroke-2 text-primary",
            )}
            aria-hidden="true"
          />
          <span className="min-w-0 truncate">{tab.label}</span>
        </Link>
      </li>
    );
  };

  const renderConsoleUtility = (onNavigate?: () => void) => {
    const isActive = location.startsWith("/debug");

    return (
      <li key="console">
        <Link
          href="/debug"
          onPointerEnter={() => handleNavIntent("/debug")}
          onPointerDown={() => handleNavIntent("/debug")}
          onFocus={() => handleNavIntent("/debug")}
          onClick={onNavigate}
          aria-current={isActive ? "page" : undefined}
          data-active={isActive ? "true" : "false"}
          className={navigationItemClassName(isActive)}
        >
          {renderActiveIndicator(isActive)}
          <Terminal
            className={cn(
              "h-[18px] w-[18px] shrink-0 stroke-[1.75px] transition-colors",
              isActive && "stroke-2 text-primary",
            )}
            aria-hidden="true"
          />
          <span className="min-w-0 truncate">{t("Console")}</span>
        </Link>
      </li>
    );
  };

  const renderTabList = (tabs: NavigationItem[], onNavigate?: () => void) => (
    <ul className="space-y-1">{tabs.map((tab) => renderTabItem(tab, onNavigate))}</ul>
  );

  const renderNav = (onNavigate?: () => void) => (
    <nav className="flex min-h-0 flex-1 flex-col px-3 pt-1" aria-label={t("Main navigation")}>
      {renderTabList(primaryTabs, onNavigate)}
      <ul className="mt-auto space-y-1 border-t border-border/60 pt-3">
        {utilityTabs.map((tab) => renderTabItem(tab, onNavigate))}
        {renderConsoleUtility(onNavigate)}
      </ul>
    </nav>
  );

  return (
    <div className="app-window-frame flex min-h-[100dvh] flex-col overflow-hidden bg-sidebar font-sans md:h-[100dvh]">
      <a
        href="#main-content"
        className={cn(
          "sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-[60]",
          "rounded-md bg-background px-3 py-2 text-sm text-foreground shadow-md",
        )}
      >
        {t("Skip to main content")}
      </a>

      <DesktopTitleBar />
      <ActiveMeetingPill />

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-sidebar md:flex-row">
        {/* Mobile Header */}
        <header className="flex items-center justify-between border-b border-border/60 bg-sidebar px-3 py-2 md:hidden">
          <div className="flex min-w-0 items-center gap-1.5">
            <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
              <SheetTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="min-h-[44px] min-w-[44px]"
                  aria-label={t("Open navigation")}
                >
                  <Menu className="h-5 w-5" />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-[280px] border-r border-border/60 bg-sidebar p-0">
                <SheetTitle className="sr-only">{t("Main navigation")}</SheetTitle>
                <div className="flex h-full flex-col">
                  <div className="flex items-center gap-2.5 px-4 pb-3 pt-5">
                    <BrandMark className="h-9 w-9" />
                    <span className="font-heading text-lg font-semibold tracking-tight text-foreground">Scriber</span>
                  </div>
                  <div className="px-3 pb-3">
                    <SidebarSearch placeholder={t("Search")} onOpenCommandPalette={handleOpenCommandPaletteFromSheet} />
                  </div>
                  {renderNav(() => setMobileNavOpen(false))}
                  <div className="mx-3 flex items-center gap-2 px-1 pb-5 pt-3">
                    <LanguageToggle />
                    <ThemeToggle align="edge" />
                  </div>
                </div>
              </SheetContent>
            </Sheet>
            <BrandMark className="h-8 w-8 shrink-0" decorative />
            <span className="truncate font-heading text-base font-semibold tracking-tight">Scriber</span>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="min-h-[44px] min-w-[44px]"
            onClick={handleOpenCommandPalette}
            aria-label={t("Open command palette")}
          >
            <Search className="h-[18px] w-[18px]" />
          </Button>
        </header>

        {/* Left Sidebar */}
        <aside className="hidden w-64 shrink-0 flex-col bg-sidebar md:flex">
          <div className="flex items-center gap-2.5 px-4 pb-3 pt-5">
            <BrandMark className="h-9 w-9" />
            <span className="font-heading text-lg font-semibold tracking-tight text-foreground">Scriber</span>
          </div>

          <div className="px-3 pb-3">
            <SidebarSearch placeholder={t("Search")} onOpenCommandPalette={handleOpenCommandPalette} />
          </div>

          {renderNav()}

          <div className="mx-3 flex items-center gap-2 px-1 pb-5 pt-3">
            <LanguageToggle />
            <ThemeToggle align="edge" />
          </div>
        </aside>

        {/* Main Content Area */}
        <main id="main-content" className="flex min-h-0 min-w-0 flex-1 flex-col pb-3 md:py-3 md:pr-3">
          <div className="relative min-w-0 flex-1 overflow-hidden bg-background md:rounded-xl md:border md:border-border/80 md:shadow-sm">
            <div
              ref={scrollContainerRef}
              className="app-scroll-viewport h-full min-w-0 overflow-x-hidden overflow-y-auto"
              data-app-scroll-container="true"
            >
              <div className="min-h-full min-w-0 bg-background">
                <AppScrollContainerContext.Provider value={scrollContainerRef}>
                  {children}
                </AppScrollContainerContext.Provider>
              </div>
            </div>
            <AppOverlayScrollbar scrollContainerRef={scrollContainerRef} />
          </div>
        </main>
      </div>

      <Suspense fallback={null}>
        {commandOpen && <CommandPalette open={commandOpen} onOpenChange={setCommandOpen} />}
      </Suspense>
    </div>
  );
}
