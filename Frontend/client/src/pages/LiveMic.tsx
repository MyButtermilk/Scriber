import { useEffect, useState } from "react";
import { Mic, Square, Clock, MoreVertical, Globe, Timer } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { Transcript } from "@/lib/mockData";
import { useLocation } from "wouter";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiUrl, wsUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";

export default function LiveMic() {
  const { toast } = useToast();
  const [isRecording, setIsRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [status, setStatus] = useState<string>("Stopped");
  const [audioLevel, setAudioLevel] = useState(0);
  const [finalText, setFinalText] = useState("");
  const [interimText, setInterimText] = useState("");
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();

  const transcriptsQuery = useQuery({
    queryKey: ["/api/transcripts"],
  });
  const transcripts: Transcript[] = (transcriptsQuery.data as any)?.items || [];

  // Mock timer
  useEffect(() => {
    let interval: NodeJS.Timeout;
    if (isRecording) {
      interval = setInterval(() => {
        setElapsed(e => e + 1);
      }, 1000);
    } else {
      setElapsed(0);
    }
    return () => clearInterval(interval);
  }, [isRecording]);

  useEffect(() => {
    const ws = new WebSocket(wsUrl("/ws"));

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (!msg || typeof msg !== "object") return;

        switch (msg.type) {
          case "state":
            setIsRecording(!!msg.listening);
            setStatus(msg.status || "Stopped");
            if (msg.current?.content) {
              setFinalText(String(msg.current.content));
              setInterimText("");
            }
            break;
          case "status":
            setIsRecording(!!msg.listening);
            setStatus(msg.status || "Stopped");
            break;
          case "audio_level":
            setAudioLevel(Number(msg.rms) || 0);
            break;
          case "transcript":
            if (msg.isFinal) {
              if (msg.content) {
                setFinalText(String(msg.content));
                setInterimText("");
                break;
              }
              const t = String(msg.text || "").trim();
              if (t) setFinalText((prev) => (prev ? `${prev} ${t}` : t));
              setInterimText("");
            } else {
              setInterimText(String(msg.text || ""));
            }
            break;
          case "session_started":
            setIsRecording(true);
            setStatus("Listening");
            setFinalText("");
            setInterimText("");
            break;
          case "session_finished":
            setIsRecording(false);
            setStatus("Stopped");
            if (msg.session?.content) {
              setFinalText(String(msg.session.content));
              setInterimText("");
            }
            queryClient.invalidateQueries({ queryKey: ["/api/transcripts"] });
            break;
          case "history_updated":
            queryClient.invalidateQueries({ queryKey: ["/api/transcripts"] });
            break;
          case "settings_updated":
            break;
          default:
            break;
        }
      } catch {
        // ignore
      }
    };

    ws.onerror = () => {
      toast({
        title: "Backend disconnected",
        description: "Could not connect to the Scriber backend. Start `python -m src.web_api`.",
        duration: 4000,
      });
    };

    return () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
  }, [queryClient, toast]);

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  const intensity = Math.min(1, Math.max(0, audioLevel * 3));
  const liveText = interimText || finalText;

  const handleToggle = async () => {
    try {
      const endpoint = isRecording ? "/api/live-mic/stop" : "/api/live-mic/start";
      const res = await fetch(apiUrl(endpoint), { method: "POST", credentials: "include" });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || res.statusText);
      }
    } catch (e: any) {
      toast({
        title: "Action failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  return (
    <div className="max-w-screen-md mx-auto px-4 py-6 md:py-8">
      <header className="mb-8 text-center space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">Live Transcription</h1>
        <p className="text-muted-foreground">Capture high-fidelity voice notes instantly</p>
      </header>

      {/* Main Recording Area */}
      <div className="flex flex-col items-center justify-center space-y-6 mb-10">
        
        {/* Live Text Output */}
        <div className="w-full max-w-lg min-h-[120px] text-center flex items-center justify-center p-6 rounded-2xl bg-secondary/30 border border-border/50 backdrop-blur-sm">
          {isRecording ? (
            <motion.p 
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="text-lg md:text-xl font-medium leading-relaxed text-foreground/90"
            >
              {liveText ? `"${liveText}"` : `"${status || "Listening"}..."`}
            </motion.p>
          ) : (
            <p className="text-muted-foreground">Ready to record. Tap the microphone to start.</p>
          )}
        </div>

        {/* Waveform Visualization (Mock) */}
        <div className="h-16 flex items-center justify-center gap-1 w-full max-w-xs overflow-hidden">
          {isRecording ? (
            Array.from({ length: 20 }).map((_, i) => (
              <motion.div
                key={i}
                className="w-1.5 bg-primary/80 rounded-full"
                animate={{
                  height: [
                    10,
                    10 + intensity * 48 * (0.35 + 0.65 * Math.abs(Math.sin((i + 1) * 0.9))),
                    10,
                  ],
                }}
                transition={{
                  repeat: Infinity,
                  duration: 0.5,
                  delay: i * 0.05,
                }}
              />
            ))
          ) : (
             <div className="w-full h-0.5 bg-border rounded-full" />
          )}
        </div>

        {/* Controls */}
        <div className="flex items-center gap-6">
           <div className="text-sm font-medium text-muted-foreground w-16 text-right">
             {isRecording && <span className="animate-pulse text-red-500">REC</span>}
           </div>

           <Button
             size="lg"
             className={`h-24 w-24 rounded-full shadow-xl transition-all duration-300 ${isRecording ? 'bg-destructive hover:bg-destructive/90 ring-4 ring-destructive/20' : 'bg-primary hover:bg-primary/90 hover:scale-105 shadow-primary/25'}`}
             onClick={handleToggle}
           >
             {isRecording ? (
               <Square className="w-12 h-12 fill-current" />
             ) : (
               <Mic className="w-16 h-16" />
             )}
           </Button>

           <div className="text-sm font-mono font-medium text-muted-foreground w-16">
             {isRecording ? formatTime(elapsed) : "00:00"}
           </div>
        </div>
      </div>

      {/* History Section */}
      <div className="space-y-4">
        <div className="flex items-center justify-between px-2">
          <h2 className="text-lg font-semibold text-foreground">Recent Recordings</h2>
          <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">View All</Button>
        </div>

        <div className="grid gap-3">
          {transcripts.filter(t => t.type === 'mic').map((item) => (
            <Card 
              key={item.id} 
              className="p-4 hover:shadow-md transition-shadow cursor-pointer border-border/60 bg-card/50 backdrop-blur-sm group"
              onClick={() => setLocation(`/transcript/${item.id}`)}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-4">
                  <div className="w-10 h-10 rounded-full bg-blue-50 dark:bg-blue-900/20 flex items-center justify-center text-primary">
                    <Mic className="w-5 h-5" />
                  </div>
                  <div>
                    <h3 className="font-medium text-foreground group-hover:text-primary transition-colors">{item.title}</h3>
                    <div className="flex items-center gap-3 text-xs text-muted-foreground mt-1">
                      <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> {item.date}</span>
                      <span className="flex items-center gap-1"><Timer className="w-3 h-3" /> {item.duration}</span>
                      <span className="flex items-center gap-1 bg-secondary px-1.5 py-0.5 rounded-md"><Globe className="w-3 h-3" /> {item.language}</span>
                    </div>
                  </div>
                </div>
                <Button variant="ghost" size="icon" className="opacity-0 group-hover:opacity-100 transition-opacity">
                  <MoreVertical className="w-4 h-4 text-muted-foreground" />
                </Button>
              </div>
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
}
