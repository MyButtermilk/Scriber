import { Link, useLocation, Router } from "wouter";
import { FileText, Mic, Settings, Youtube } from "lucide-react";
import { cn } from "@/lib/utils";
import { motion, AnimatePresence } from "framer-motion";
import { ThemeToggle } from "@/components/theme-toggle";

interface AppLayoutProps {
  children: React.ReactNode;
  path?: string;
}

export function AppLayout({ children, path }: AppLayoutProps) {
  const [location, setLocation] = useLocation();
  const currentKey = path || location;

  const tabs = [
    { href: "/", icon: Mic, label: "Live Mic" },
    { href: "/youtube", icon: Youtube, label: "Youtube" },
    { href: "/file", icon: FileText, label: "File" },
    { href: "/settings", icon: Settings, label: "Settings" },
  ];

  return (
    <div className="h-screen overflow-hidden bg-background text-foreground flex flex-col font-sans">
      {/* Header with theme toggle */}
      <header className="sticky top-0 z-40 flex items-center justify-between px-4 py-2 border-b border-border/40 bg-background/80 backdrop-blur-xl">
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" alt="Scriber" className="w-8 h-8" />
          <span className="font-heading font-semibold text-lg">Scriber</span>
        </div>
        <ThemeToggle />
      </header>

      <main className="flex-1 overflow-y-auto pb-32">
        <AnimatePresence mode="wait">
          <motion.div
            key={currentKey}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            transition={{ duration: 0.4, ease: "easeOut" }}
            className="min-h-full"
          >
            <Router hook={() => [currentKey, setLocation]}>
              {children}
            </Router>
          </motion.div>
        </AnimatePresence>
      </main>

      {/* Glassmorphism navigation bar */}
      <nav className="fixed bottom-0 left-0 right-0 z-50 pb-4 pt-2 pr-2 safe-area-bottom pointer-events-none">
        <div className="max-w-screen-md mx-auto px-4 pointer-events-auto">
          <div className="flex justify-evenly items-center h-16 rounded-2xl bg-white/70 dark:bg-zinc-900/70 backdrop-blur-xl border border-white/20 dark:border-white/10 shadow-xl shadow-black/10 dark:shadow-black/30">
            <AnimatePresence>
              {tabs.map((tab) => {
                const isActive = location === tab.href || (tab.href !== "/" && location.startsWith(tab.href));
                const Icon = tab.icon;

                return (
                  <Link key={tab.href} href={tab.href} className={cn(
                    "relative flex flex-col items-center justify-center space-y-1 w-20 transition-colors duration-200 group cursor-pointer no-underline outline-none focus-visible:ring-2 focus-visible:ring-primary rounded-xl",
                    isActive ? "text-primary" : "text-muted-foreground hover:text-foreground"
                  )}>
                    <div className="relative p-1.5 rounded-xl z-10">
                      {isActive && (
                        <motion.div
                          layoutId="nav-indicator"
                          className="absolute inset-0 bg-primary/10 dark:bg-primary/20 rounded-xl"
                          transition={{ type: "spring", stiffness: 300, damping: 30 }}
                        />
                      )}
                      <Icon className={cn("relative w-6 h-6 transition-all duration-300", isActive && "stroke-[2.5px] scale-110")} />
                    </div>
                    <span className="text-[10px] font-medium tracking-wide uppercase z-10">{tab.label}</span>
                  </Link>
                );
              })}
            </AnimatePresence>
          </div>
        </div>
      </nav>
    </div>
  );
}

