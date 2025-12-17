import { useParams, Link } from "wouter";
import { ArrowLeft, Share2, Download, Copy, Play, Search, Clock, Calendar, Pencil, Check, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { MOCK_TRANSCRIPTS } from "@/lib/mockData";
import { useToast } from "@/hooks/use-toast";
import { useState, useEffect } from "react";
import { wsUrl } from "@/lib/backend";

export default function TranscriptDetail() {
  const { id } = useParams();
  const { toast } = useToast();
  const [copied, setCopied] = useState(false);
  const queryClient = useQueryClient();

  const transcriptQuery = useQuery({
    queryKey: ["/api/transcripts", id],
    enabled: !!id,
    refetchInterval: (data: any) => {
      const status = data?.status;
      return status === "processing" || status === "recording" ? 1000 : false;
    },
  });
  const mock = MOCK_TRANSCRIPTS.find((t) => t.id === id);
  const transcript: any = transcriptQuery.data || mock || {
    title: "Transcript",
    date: "",
    duration: "",
    content: "",
    type: "mic",
  };

  // WebSocket connection for real-time updates
  useEffect(() => {
    const ws = new WebSocket(wsUrl("/ws"));

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg?.type === "history_updated") {
          // Invalidate this specific transcript query to refresh content
          queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] });
        }
      } catch {
        // ignore parse errors
      }
    };

    return () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
  }, [id, queryClient]);

  const handleCopy = () => {
    navigator.clipboard.writeText(transcript?.content || "");
    setCopied(true);
    toast({
      title: "Copied to Clipboard",
      description: "Transcript content has been copied.",
      duration: 2000,
    });
    setTimeout(() => setCopied(false), 2000);
  };

  const getBackLink = () => {
    switch (transcript?.type) {
      case "youtube":
        return "/youtube";
      case "file":
        return "/file";
      default:
        return "/";
    }
  };

  return (
    <div className="min-h-screen bg-background flex flex-col">
      {/* Header Toolbar */}
      <header className="sticky top-0 z-40 bg-background/80 backdrop-blur-md border-b border-border h-16 flex items-center justify-between px-4 md:px-8">
        <div className="flex items-center gap-4">
          <Link href={getBackLink()}>
            <Button variant="ghost" size="icon" className="-ml-2">
              <ArrowLeft className="w-5 h-5 text-muted-foreground" />
            </Button>
          </Link>
          <div>
            <h1 className="font-semibold text-lg leading-tight">{transcript?.title || "Transcript"}</h1>
            <p className="text-xs text-muted-foreground">{transcript.date} • {transcript.duration}</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="hidden md:flex">
            <Download className="w-4 h-4 mr-2" /> Export
          </Button>
          <Button variant="outline" size="sm" className="hidden md:flex">
            <Share2 className="w-4 h-4 mr-2" /> Share
          </Button>
          <Button size="sm" className="bg-primary text-primary-foreground">
            <Play className="w-4 h-4 mr-2" /> Play Audio
          </Button>
        </div>
      </header>

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">

        {/* Transcript Area */}
        <main className="flex-1 overflow-y-auto p-4 md:p-8 md:px-16 lg:px-32">
          <div className="max-w-3xl mx-auto space-y-8">

            {/* Meta Card */}
            <div className="flex flex-wrap gap-2 mb-8">
              <Badge variant="secondary" className="px-3 py-1 font-normal"><Clock className="w-3 h-3 mr-1.5" /> {transcript.duration}</Badge>
              <Badge variant="secondary" className="px-3 py-1 font-normal"><Calendar className="w-3 h-3 mr-1.5" /> {transcript.date}</Badge>
            </div>

            {/* Speaker Blocks */}
            <div className="space-y-8 text-lg leading-relaxed text-foreground/90">
              <div className="group">
                <div className="flex items-baseline justify-between mb-2">
                  <span className="font-semibold text-sm text-blue-600 uppercase tracking-wide">Transcript</span>
                  {transcript.status === "processing" && transcript.step && (
                    <span className="flex items-center gap-2 text-sm text-muted-foreground">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      {transcript.step}
                    </span>
                  )}
                </div>
                <p className="pl-4 border-l-2 border-border/50 whitespace-pre-wrap">
                  {transcriptQuery.isLoading ? (
                    "Loading..."
                  ) : transcript.status === "processing" ? (
                    <span className="text-muted-foreground italic">
                      {transcript.step || "Processing..."}
                    </span>
                  ) : transcript.content ? (
                    transcript.content
                  ) : (
                    "No transcript text captured."
                  )}
                </p>
              </div>
            </div>

          </div>
        </main>

        {/* Sidebar (Desktop only) */}
        <aside className="w-80 border-l border-border bg-secondary/10 hidden xl:block p-6 overflow-y-auto">
          <div className="space-y-6">
            <div>
              <h3 className="text-sm font-semibold mb-3">Summary</h3>
              <p className="text-sm text-muted-foreground leading-relaxed">
                The meeting covered the Q4 roadmap updates, with a focus on the simplified navigation hierarchy and mobile responsiveness. Designs have been finalized.
              </p>
            </div>

            <Separator />

            <div>
              <h3 className="text-sm font-semibold mb-3">Keywords</h3>
              <div className="flex flex-wrap gap-2">
                {["Roadmap", "Q4", "Navigation", "Mobile", "Design", "Feedback"].map(tag => (
                  <Badge key={tag} variant="secondary" className="cursor-pointer hover:bg-secondary/80">{tag}</Badge>
                ))}
              </div>
            </div>

            <Separator />

            <div>
              <h3 className="text-sm font-semibold mb-3">Actions</h3>
              <Button
                className="w-full mb-2 transition-all duration-200"
                variant={copied ? "default" : "outline"}
                onClick={handleCopy}
              >
                {copied ? <Check className="w-4 h-4 mr-2" /> : <Copy className="w-4 h-4 mr-2" />}
                {copied ? "Copied!" : "Copy Text"}
              </Button>
              <Button className="w-full" variant="outline"><Download className="w-4 h-4 mr-2" /> Download PDF</Button>
            </div>
          </div>
        </aside>

      </div>
    </div>
  );
}
