import { Link, useLocation, Router } from "wouter";
import { Mic, Settings, Youtube, FolderOpen } from "lucide-react";
import { cn } from "@/lib/utils";
import { motion, AnimatePresence } from "framer-motion";
import { SidebarSearch } from "@/components/ui/sidebar-search";
import { ThemeToggle } from "@/components/theme-toggle";
import { useQueryClient } from "@tanstack/react-query";
import { useState, useEffect, useCallback } from "react";
import { CommandPalette } from "@/components/CommandPalette";

interface AppLayoutProps {
  children: React.ReactNode;
  path?: string;
}

export function AppLayout({ children, path }: AppLayoutProps) {
  const [location, setLocation] = useLocation();
  const currentKey = path || location;
  const queryClient = useQueryClient();
  const [commandOpen, setCommandOpen] = useState(false);

  // Global Strg+K handler for Command Palette
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setCommandOpen((open) => !open);
      }
    };
    document.addEventListener("keydown", down);
    return () => document.removeEventListener("keydown", down);
  }, []);

  // Callback for SidebarSearch to open Command Palette
  const handleOpenCommandPalette = useCallback(() => {
    setCommandOpen(true);
  }, []);

  // Prefetch transcripts data on nav hover for instant loading
  const handleNavHover = () => {
    queryClient.prefetchQuery({ queryKey: ["/api/transcripts"] });
  };

  const tabs = [
    { href: "/", icon: Mic, label: "Live Mic" },
    { href: "/youtube", icon: Youtube, label: "YouTube" },
    { href: "/file", icon: FolderOpen, label: "File" },
    { href: "/settings", icon: Settings, label: "Settings" },
  ];

  return (
    <div className="h-screen overflow-hidden bg-sidebar font-sans flex">
      {/* Left Sidebar - extends to screen edges */}
      <aside className="w-60 md:w-64 shrink-0 flex flex-col">
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
        <nav className="flex-1 px-3 pt-1">
          <ul className="space-y-1.5">
            {tabs.map((tab) => {
              const isActive = location === tab.href || (tab.href !== "/" && location.startsWith(tab.href));
              const Icon = tab.icon;

              return (
                <li key={tab.href}>
                  <Link
                    href={tab.href}
                    onMouseEnter={handleNavHover}
                    className={cn(
                      "neu-nav-item flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium cursor-pointer no-underline outline-none",
                      isActive
                        ? "neu-nav-active text-foreground"
                        : "text-muted-foreground hover:text-foreground"
                    )}
                  >
                    <Icon className={cn(
                      "w-5 h-5 shrink-0 stroke-[1.5px]",
                      isActive && "stroke-[2px]"
                    )} />
                    <span>{tab.label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        {/* Theme Toggle at bottom */}
        <div className="px-3 pb-4 pt-2">
          <ThemeToggle />
        </div>
      </aside>

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col py-3 pr-3">
        {/* Content panel - rounded, inset within the sidebar-colored background */}
        <div className="flex-1 bg-card rounded-xl overflow-hidden neu-panel-inset">
          <div className="h-full overflow-y-auto">
            <AnimatePresence mode="wait">
              <motion.div
                key={currentKey}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{
                  duration: 0.15,
                  ease: "easeOut"
                }}
                className="min-h-full"
              >
                <Router hook={() => [currentKey, setLocation]}>
                  {children}
                </Router>
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      </main>

      {/* Command Palette */}
      <CommandPalette open={commandOpen} onOpenChange={setCommandOpen} />
    </div>
  );
}
