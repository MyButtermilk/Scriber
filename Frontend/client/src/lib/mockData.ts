import { Mic, Youtube, FileText, Settings, Play, Pause, Upload, Search, MoreVertical, Clock, CheckCircle, AlertCircle, Trash2, Share2, FileDown, Pencil, ChevronLeft, Calendar } from "lucide-react";

export type TranscriptionStatus = "completed" | "processing" | "failed" | "recording";

export interface Transcript {
  id: string;
  title: string;
  date: string;
  duration: string;
  status: TranscriptionStatus;
  type: "mic" | "youtube" | "file";
  content?: string;
  language?: string;
  channel?: string; // for youtube
  fileSize?: string; // for files
}

export const MOCK_TRANSCRIPTS: Transcript[] = [
  {
    id: "1",
    title: "Quarterly Business Review Q4",
    date: "Today, 10:30 AM",
    duration: "45:20",
    status: "completed",
    type: "mic",
    language: "English (US)",
    content: "Thank you everyone for joining. Today we're going to discuss the Q4 results..."
  },
  {
    id: "2",
    title: "Product Design Sync",
    date: "Yesterday",
    duration: "22:15",
    status: "completed",
    type: "mic",
    language: "English (US)",
    content: "Let's look at the new figma mocks for the dashboard..."
  }
];

