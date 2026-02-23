import { Link, useLocation, Router } from "wouter";
import { Mic, Settings, Youtube, FolderOpen, Menu, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { SidebarSearch } from "@/components/ui/sidebar-search";
import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { useState, useEffect, useCallback, lazy, Suspense } from "react";

const CommandPalette = lazy(async () => {
  const module = await import("@/components/CommandPalette");
  return { default: module.CommandPalette };
});

interface AppLayoutProps {
  children: React.ReactNode;
  path?: string;
}

export function AppLayout({ children, path }: AppLayoutProps) {
  const [location, setLocation] = useLocation();
  const currentKey = path || location;
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

  const preloadRouteChunk = useCallback((href: string) => {
    if (href === "/youtube") {
      void import("@/pages/Youtube");
      return;
    }
    if (href === "/file") {
      void import("@/pages/FileTranscribe");
      return;
    }
    if (href === "/settings") {
      void import("@/pages/Settings");
    }
  }, []);

  // Preload route chunks on intent to keep navigation responsive.
  const handleNavHover = (href: string) => {
    preloadRouteChunk(href);
  };

  const tabs = [
    { href: "/", icon: Mic, label: "Live Mic" },
    { href: "/youtube", icon: Youtube, label: "YouTube" },
    { href: "/file", icon: FolderOpen, label: "File" },
    { href: "/settings", icon: Settings, label: "Settings" },
  ];

  const renderNav = (onNavigate?: () => void) => (
    <nav className="flex-1 px-3 pt-1">
      <ul className="space-y-1.5">
        {tabs.map((tab) => {
          const isActive = location === tab.href || (tab.href !== "/" && location.startsWith(tab.href));
          const Icon = tab.icon;

          return (
            <li key={tab.href}>
              <Link
                href={tab.href}
                onMouseEnter={() => handleNavHover(tab.href)}
                onFocus={() => handleNavHover(tab.href)}
                onClick={onNavigate}
                className={cn(
                  "neu-nav-item flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium cursor-pointer no-underline outline-none",
                  isActive
                    ? "neu-nav-active text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon
                  className={cn(
                    "w-5 h-5 shrink-0 stroke-[1.5px]",
                    isActive && "stroke-[2px]",
                  )}
                />
                <span>{tab.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );

  return (
    <div className="min-h-screen md:h-screen overflow-hidden bg-sidebar font-sans flex flex-col md:flex-row">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-[60] rounded-md bg-background px-3 py-2 text-sm text-foreground shadow-md"
      >
        Skip to main content
      </a>

      {/* Mobile Header */}
      <header className="md:hidden flex items-center justify-between border-b border-border/50 px-3 py-2">
        <div className="flex items-center gap-1.5">
          <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
            <SheetTrigger asChild>
              <Button type="button" variant="ghost" size="icon" aria-label="Open navigation">
                <Menu className="h-5 w-5" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-[280px] border-r border-border/50 bg-sidebar p-0">
              <SheetTitle className="sr-only">Main navigation</SheetTitle>
              <div className="flex h-full flex-col">
                <div className="px-4 pt-5 pb-3 flex items-center gap-2.5">
                  <img src="/favicon.svg" alt="Scriber" className="w-7 h-7" />
                  <span className="font-heading font-semibold text-lg text-foreground tracking-tight">Scriber</span>
                </div>
                <div className="px-3 pb-3">
                  <SidebarSearch placeholder="Search" onOpenCommandPalette={handleOpenCommandPaletteFromSheet} />
                </div>
                {renderNav(() => setMobileNavOpen(false))}
                <div className="px-4 pb-5 pt-2">
                  <ThemeToggle align="edge" />
                </div>
              </div>
            </SheetContent>
          </Sheet>
          <img src="/favicon.svg" alt="" className="h-6 w-6" aria-hidden="true" />
          <span className="font-heading text-base font-semibold tracking-tight">Scriber</span>
        </div>
        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={handleOpenCommandPalette}
            aria-label="Open command palette"
          >
            <Search className="h-4 w-4" />
          </Button>
          <ThemeToggle />
        </div>
      </header>

      {/* Left Sidebar - extends to screen edges */}
      <aside className="hidden md:flex w-60 md:w-64 shrink-0 flex-col">
        {/* Logo and Branding */}
        <div className="px-4 pt-5 pb-3 flex items-center gap-2.5">
          <img src="/favicon.svg" alt="Scriber" className="w-7 h-7" />
          <span className="font-heading font-semibold text-lg text-foreground tracking-tight">Scriber</span>
        </div>

        {/* Search Bar */}
        <div className="px-3 pb-3">
          <SidebarSearch placeholder="Search" onOpenCommandPalette={handleOpenCommandPalette} />
        </div>

        {/* Navigation */}
        {renderNav()}

        {/* Theme Toggle at bottom */}
        <div className="px-4 pb-5 pt-2">
          <ThemeToggle align="edge" />
        </div>
      </aside>

      {/* Main Content Area */}
      <main id="main-content" className="min-h-0 flex-1 flex flex-col pb-3 md:py-3 md:pr-3">
        {/* Content panel - rounded, inset within the sidebar-colored background */}
        <div className="flex-1 overflow-hidden md:bg-card md:rounded-xl md:neu-panel-inset">
          <div className="h-full overflow-y-auto">
            <div key={currentKey} className="min-h-full">
              <Router hook={() => [currentKey, setLocation]}>
                {children}
              </Router>
            </div>
          </div>
        </div>
      </main>

      {/* Command Palette */}
      <Suspense fallback={null}>
        {commandOpen && <CommandPalette open={commandOpen} onOpenChange={setCommandOpen} />}
      </Suspense>
    </div>
  );
}
