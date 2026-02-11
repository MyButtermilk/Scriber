import { Mic, Youtube, UploadCloud, FileText } from "lucide-react";

interface EmptyStateProps {
    type: "mic" | "youtube" | "file" | "transcript";
    title?: string;
    description?: string;
}

const iconMap = {
    mic: Mic,
    youtube: Youtube,
    file: UploadCloud,
    transcript: FileText,
};

const defaultContent = {
    mic: {
        title: "No recordings yet",
        description: "Tap the microphone button to start recording your first transcript.",
    },
    youtube: {
        title: "No videos transcribed",
        description: "Search for a YouTube video or paste a URL to get started.",
    },
    file: {
        title: "No files uploaded",
        description: "Drag and drop audio or video files here to transcribe them.",
    },
    transcript: {
        title: "Transcript not found",
        description: "This transcript may have been deleted or doesn't exist.",
    },
};

export function EmptyState({ type, title, description }: EmptyStateProps) {
    const Icon = iconMap[type];
    const content = defaultContent[type];

    return (
        <div className="empty-state-container">
            <div className="neu-search-inset p-6 rounded-full">
                <Icon className="empty-state-icon text-muted-foreground" />
            </div>
            <h3 className="empty-state-title">{title || content.title}</h3>
            <p className="empty-state-description">{description || content.description}</p>
        </div>
    );
}
