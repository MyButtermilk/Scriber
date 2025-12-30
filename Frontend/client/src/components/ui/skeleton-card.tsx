import { cn } from "@/lib/utils";

interface SkeletonCardProps {
    className?: string;
    variant?: "list" | "grid";
}

export function SkeletonCard({ className, variant = "list" }: SkeletonCardProps) {
    if (variant === "grid") {
        return (
            <div
                className={cn(
                    "neu-skeleton-card rounded-xl p-4 bg-card",
                    className
                )}
            >
                {/* Thumbnail */}
                <div className="neu-skeleton w-full h-24 rounded-lg mb-3" />
                {/* Title */}
                <div className="neu-skeleton h-4 w-3/4 mb-2" />
                {/* Meta */}
                <div className="flex gap-2">
                    <div className="neu-skeleton h-3 w-12" />
                    <div className="neu-skeleton h-3 w-16" />
                </div>
            </div>
        );
    }

    return (
        <div
            className={cn(
                "neu-skeleton-card rounded-xl p-4 bg-card flex items-center gap-4",
                className
            )}
        >
            {/* Icon/Thumbnail placeholder */}
            <div className="neu-skeleton w-12 h-12 rounded-lg shrink-0" />
            {/* Content */}
            <div className="flex-1 space-y-2">
                <div className="neu-skeleton h-4 w-2/3" />
                <div className="flex gap-3">
                    <div className="neu-skeleton h-3 w-16" />
                    <div className="neu-skeleton h-3 w-20" />
                </div>
            </div>
            {/* Action button placeholder */}
            <div className="neu-skeleton w-8 h-8 rounded-lg shrink-0" />
        </div>
    );
}

interface SkeletonListProps {
    count?: number;
    variant?: "list" | "grid";
    className?: string;
}

export function SkeletonList({ count = 3, variant = "list", className }: SkeletonListProps) {
    return (
        <div
            className={cn(
                variant === "grid" ? "grid grid-cols-2 gap-3" : "space-y-3",
                className
            )}
        >
            {Array.from({ length: count }).map((_, i) => (
                <SkeletonCard key={i} variant={variant} />
            ))}
        </div>
    );
}
