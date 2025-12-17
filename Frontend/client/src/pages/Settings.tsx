import { User, CreditCard, Keyboard, Shield, Zap, Globe, ChevronRight, LogOut, Eye, EyeOff, Check, Mic, Mic2, MousePointerClick, ToggleLeft, AudioLines } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useState, useEffect } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogDescription } from "@/components/ui/dialog";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import { Textarea } from "@/components/ui/textarea";
import { apiUrl } from "@/lib/backend";

export default function Settings() {
  const [openAIKey, setOpenAIKey] = useState("");
  const [deepgramKey, setDeepgramKey] = useState("");
  const [assemblyAIKey, setAssemblyAIKey] = useState("");
  const [geminiKey, setGeminiKey] = useState("");
  const [youtubeKey, setYoutubeKey] = useState("");
  const [sonioxKey, setSonioxKey] = useState("");
  const [elevenLabsKey, setElevenLabsKey] = useState("");
  const [azureKey, setAzureKey] = useState("");
  const [azureRegion, setAzureRegion] = useState("");
  const [gladiaKey, setGladiaKey] = useState("");
  const [groqKey, setGroqKey] = useState("");
  const [awsKey, setAwsKey] = useState("");

  const [customVocabulary, setCustomVocabulary] = useState("");

  const [showOpenAIKey, setShowOpenAIKey] = useState(false);
  const [showDeepgramKey, setShowDeepgramKey] = useState(false);
  const [showAssemblyAIKey, setShowAssemblyAIKey] = useState(false);
  const [showGeminiKey, setShowGeminiKey] = useState(false);
  const [showYoutubeKey, setShowYoutubeKey] = useState(false);
  const [showSonioxKey, setShowSonioxKey] = useState(false);
  const [showElevenLabsKey, setShowElevenLabsKey] = useState(false);
  const [showAzureKey, setShowAzureKey] = useState(false);
  const [showGladiaKey, setShowGladiaKey] = useState(false);
  const [showGroqKey, setShowGroqKey] = useState(false);
  const [showAwsKey, setShowAwsKey] = useState(false);

  const [hotkey, setHotkey] = useState("Ctrl + Shift + S");
  const [recordingMode, setRecordingMode] = useState("press_hold");
  const [isRecordingHotkey, setIsRecordingHotkey] = useState(false);
  const { toast } = useToast();
  const [savedKeys, setSavedKeys] = useState<Record<string, boolean>>({});

  const [inputDevices, setInputDevices] = useState<{deviceId: string, label: string}[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("default");
  const [transcriptionModel, setTranscriptionModel] = useState("soniox-realtime");
  const [language, setLanguage] = useState("auto");

  useEffect(() => {
    let cancelled = false;

    const serviceToModel = (service: string, sonioxMode: string) => {
      if (service === "soniox" || service === "soniox_async") {
        return sonioxMode === "async" || service === "soniox_async"
          ? "soniox-async"
          : "soniox-realtime";
      }
      return service || "soniox-realtime";
    };

    const load = async () => {
      try {
        const [settingsRes, micsRes] = await Promise.all([
          fetch(apiUrl("/api/settings"), { credentials: "include" }),
          fetch(apiUrl("/api/microphones"), { credentials: "include" }),
        ]);

        if (!settingsRes.ok) throw new Error(await settingsRes.text());
        if (!micsRes.ok) throw new Error(await micsRes.text());

        const settings = await settingsRes.json();
        const mics = await micsRes.json();
        if (cancelled) return;

        const keys = settings.apiKeys || {};
        setHotkey(settings.hotkey || settings.hotkeyRaw || "");
        setRecordingMode(settings.mode === "push_to_talk" ? "press_hold" : "start_stop");
        setSelectedDeviceId(settings.micDevice || "default");
        setLanguage(settings.language || "auto");
        setTranscriptionModel(serviceToModel(settings.defaultSttService || "", settings.sonioxMode || "realtime"));
        setCustomVocabulary(settings.customVocab || "");

        setSonioxKey(keys.soniox || "");
        setAssemblyAIKey(keys.assemblyai || "");
        setDeepgramKey(keys.deepgram || "");
        setOpenAIKey(keys.openai || "");
        setGeminiKey(keys.googleApiKey || "");
        setYoutubeKey(keys.youtubeApiKey || "");
        setElevenLabsKey(keys.elevenlabs || "");
        setAzureKey(keys.azureSpeechKey || "");
        setAzureRegion(keys.azureSpeechRegion || "");
        setGladiaKey(keys.gladia || "");
        setGroqKey(keys.groq || "");

        setInputDevices((mics.devices || []) as {deviceId: string, label: string}[]);
      } catch (e: any) {
        toast({
          title: "Failed to load settings",
          description: String(e?.message || e),
          duration: 4000,
        });
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, [toast]);

  const updateSettings = async (patch: any) => {
    const res = await fetch(apiUrl("/api/settings"), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
      credentials: "include",
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || res.statusText);
    }
    return await res.json();
  };

  const handleSaveApiKey = async (provider: string) => {
    try {
      if (provider === "AWS") {
        throw new Error("AWS credentials are not managed yet (use standard AWS env vars).");
      }
      const apiKeys: Record<string, string> = {};
      if (provider === "OpenAI") apiKeys.openai = openAIKey;
      if (provider === "Deepgram") apiKeys.deepgram = deepgramKey;
      if (provider === "AssemblyAI") apiKeys.assemblyai = assemblyAIKey;
      if (provider === "Gemini") apiKeys.googleApiKey = geminiKey;
      if (provider === "YouTube") apiKeys.youtubeApiKey = youtubeKey;
      if (provider === "Soniox") apiKeys.soniox = sonioxKey;
      if (provider === "ElevenLabs") apiKeys.elevenlabs = elevenLabsKey;
      if (provider === "Azure") {
        apiKeys.azureSpeechKey = azureKey;
        apiKeys.azureSpeechRegion = azureRegion;
      }
      if (provider === "Gladia") apiKeys.gladia = gladiaKey;
      if (provider === "Groq") apiKeys.groq = groqKey;

      await updateSettings({ apiKeys });

      setSavedKeys((prev) => ({ ...prev, [provider]: true }));
      toast({
        title: "Saved",
        description: `${provider} settings updated.`,
        duration: 2000,
      });
      setTimeout(() => {
        setSavedKeys((prev) => ({ ...prev, [provider]: false }));
      }, 2000);
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleHotkeyRecord = (e: React.KeyboardEvent) => {
    e.preventDefault();
    const keys = [];
    if (e.ctrlKey) keys.push("Ctrl");
    if (e.shiftKey) keys.push("Shift");
    if (e.altKey) keys.push("Alt");
    if (e.metaKey) keys.push("Meta");
    if (e.key && !["Control", "Shift", "Alt", "Meta"].includes(e.key)) {
      keys.push(e.key.toUpperCase());
    }
    
    if (keys.length > 0) {
       setHotkey(keys.join(" + "));
    }
  };

  const handleMicDeviceChange = async (deviceId: string) => {
    setSelectedDeviceId(deviceId);
    try {
      await updateSettings({ micDevice: deviceId });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleTranscriptionModelChange = async (value: string) => {
    setTranscriptionModel(value);
    try {
      if (value === "soniox-async") {
        await updateSettings({ defaultSttService: "soniox", sonioxMode: "async" });
      } else if (value === "soniox-realtime") {
        await updateSettings({ defaultSttService: "soniox", sonioxMode: "realtime" });
      } else {
        await updateSettings({ defaultSttService: value });
      }
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleLanguageChange = async (value: string) => {
    setLanguage(value);
    try {
      await updateSettings({ language: value });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleSaveHotkey = async () => {
    try {
      const updated = await updateSettings({ hotkey });
      setHotkey(updated.hotkey || hotkey);
      toast({
        title: "Saved",
        description: "Hotkey updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      setIsRecordingHotkey(false);
    }
  };

  const handleRecordingModeChange = async (mode: string) => {
    setRecordingMode(mode);
    try {
      await updateSettings({ mode: mode === "press_hold" ? "push_to_talk" : "toggle" });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleCustomVocabBlur = async () => {
    try {
      await updateSettings({ customVocab: customVocabulary });
    } catch {
      // ignore
    }
  };

  return (
    <div className="max-w-screen-md mx-auto px-4 py-6 md:py-8">
      <header className="mb-6 space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">Settings</h1>
        <p className="text-muted-foreground">Manage your preferences and API keys</p>
      </header>
      <div className="space-y-8">
        
        {/* Transcription Settings */}
        <section className="space-y-4">
          <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-4">Transcription</h2>
          <div className="bg-card border border-border/60 rounded-xl p-6 space-y-6">
            
            <div className="flex items-center justify-between">
               <div className="space-y-0.5">
                 <Label className="text-base">Input Device</Label>
                 <p className="text-sm text-muted-foreground">Select microphone for recording</p>
                </div>
               <Select value={selectedDeviceId} onValueChange={handleMicDeviceChange}>
                  <SelectTrigger className="w-[320px]">
                    <div className="flex items-center truncate">
                      <AudioLines className="w-4 h-4 mr-2 text-muted-foreground shrink-0" />
                     <SelectValue placeholder="Select microphone" />
                   </div>
                 </SelectTrigger>
                 <SelectContent>
                   {inputDevices.map((device) => (
                     <SelectItem key={device.deviceId} value={device.deviceId}>
                       {device.label}
                     </SelectItem>
                   ))}
                 </SelectContent>
               </Select>
            </div>

            <Separator />

            <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <Label className="text-base">Transcription Model</Label>
                  <p className="text-sm text-muted-foreground">Select the AI model for live transcription</p>
                </div>
               <Select value={transcriptionModel} onValueChange={handleTranscriptionModelChange}>
                  <SelectTrigger className="w-[320px]">
                    <SelectValue placeholder="Select model" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="soniox-realtime">Soniox Realtime</SelectItem>
                    <SelectItem value="soniox-async">Soniox Async</SelectItem>
                    <SelectItem value="assemblyai">AssemblyAI</SelectItem>
                    <SelectItem value="deepgram">Deepgram</SelectItem>
                    <SelectItem value="openai">OpenAI</SelectItem>
                    <SelectItem value="azure">Azure Speech</SelectItem>
                    <SelectItem value="gladia">Gladia</SelectItem>
                    <SelectItem value="groq">Groq</SelectItem>
                    <SelectItem value="speechmatics">Speechmatics</SelectItem>
                    <SelectItem value="elevenlabs">ElevenLabs</SelectItem>
                    <SelectItem value="google">Google Cloud STT</SelectItem>
                    <SelectItem value="aws">AWS Transcribe</SelectItem>
                  </SelectContent>
                </Select>
             </div>

            <Separator />

            <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <Label className="text-base">Default Language</Label>
                  <p className="text-sm text-muted-foreground">Fallback language for detection</p>
                </div>
               <Select value={language} onValueChange={handleLanguageChange}>
                  <SelectTrigger className="w-[320px]">
                    <SelectValue placeholder="Select language" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="auto">
                     <div className="flex items-center gap-2">
                       <Globe className="w-4 h-4 text-muted-foreground" />
                       <span>Auto-detect</span>
                     </div>
                    </SelectItem>
                    <SelectItem value="en">
                      <div className="flex items-center gap-2">
                        <span className="fi fi-us rounded-sm"></span>
                        <span>English</span>
                      </div>
                    </SelectItem>
                    <SelectItem value="es">
                      <div className="flex items-center gap-2">
                        <span className="fi fi-es rounded-sm"></span>
                        <span>Spanish</span>
                     </div>
                   </SelectItem>
                   <SelectItem value="fr">
                     <div className="flex items-center gap-2">
                       <span className="fi fi-fr rounded-sm"></span>
                       <span>French</span>
                     </div>
                   </SelectItem>
                   <SelectItem value="de">
                     <div className="flex items-center gap-2">
                       <span className="fi fi-de rounded-sm"></span>
                       <span>German</span>
                     </div>
                   </SelectItem>
                   <SelectItem value="it">
                     <div className="flex items-center gap-2">
                       <span className="fi fi-it rounded-sm"></span>
                       <span>Italian</span>
                      </div>
                    </SelectItem>
                  </SelectContent>
                </Select>
             </div>

            <Separator />

            <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <Label className="text-base">Summarization Model</Label>
                  <p className="text-sm text-muted-foreground">Select model for summarizing transcripts</p>
                </div>
               <Select defaultValue="gemini-3.0-pro" disabled>
                  <SelectTrigger className="w-[320px]">
                    <SelectValue placeholder="Select model" />
                  </SelectTrigger>
                  <SelectContent>
                   <SelectItem value="gemini-3.0-pro">Gemini 3.0 Pro</SelectItem>
                   <SelectItem value="gemini-2.5-flash">Gemini 2.5 Flash</SelectItem>
                   <SelectItem value="gpt-5.2">OpenAI GPT-5.2</SelectItem>
                 </SelectContent>
               </Select>
            </div>


            <Separator />
            
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label className="text-base">Global Hotkey</Label>
                <p className="text-sm text-muted-foreground">Shortcut to start/stop recording</p>
              </div>
              
              <Dialog open={isRecordingHotkey} onOpenChange={setIsRecordingHotkey}>
                <DialogTrigger asChild>
                  <Button variant="outline" className="min-w-[140px] font-mono">
                    <Keyboard className="w-4 h-4 mr-2 text-muted-foreground" />
                    {hotkey}
                  </Button>
                </DialogTrigger>
                <DialogContent className="sm:max-w-[425px]">
                  <DialogHeader>
                    <DialogTitle>Record Hotkey</DialogTitle>
                    <DialogDescription>
                      Press the combination of keys you want to use as a shortcut.
                    </DialogDescription>
                  </DialogHeader>
                  <div 
                    className="flex items-center justify-center h-32 border-2 border-dashed rounded-lg bg-secondary/20 outline-none focus:border-primary focus:bg-primary/5 transition-colors"
                    tabIndex={0}
                    onKeyDown={handleHotkeyRecord}
                  >
                    <p className="text-lg font-medium text-primary animate-pulse">Press keys now...</p>
                  </div>
                  <div className="flex justify-end gap-2">
                   <Button variant="ghost" onClick={() => setIsRecordingHotkey(false)}>Cancel</Button>
                    <Button onClick={handleSaveHotkey}>Save</Button>
                  </div>
                </DialogContent>
              </Dialog>

            </div>

            <Separator />

            <div className="flex flex-col gap-4">
               <div className="space-y-0.5">
                 <Label className="text-base">Recording Mode</Label>
                 <p className="text-sm text-muted-foreground">Choose how the hotkey behaves</p>
               </div>
               
               <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                 <div 
                   className={cn(
                     "flex flex-col gap-2 p-4 rounded-xl border-2 cursor-pointer transition-all hover:bg-accent/5",
                     recordingMode === "press_hold" 
                       ? "border-primary bg-primary/5" 
                       : "border-border/60 hover:border-primary/50"
                    )}
                    onClick={() => handleRecordingModeChange("press_hold")}
                  >
                    <div className="flex items-center gap-3">
                      <div className={cn(
                        "p-2 rounded-lg",
                        recordingMode === "press_hold" ? "bg-primary/10 text-primary" : "bg-secondary text-muted-foreground"
                      )}>
                        <Mic className="w-5 h-5" />
                      </div>
                      <div className="space-y-1">
                        <p className="font-medium text-sm leading-none">Push and Hold</p>
                        <p className="text-xs text-muted-foreground">Hold key to record, release to stop</p>
                      </div>
                      {recordingMode === "press_hold" && <Check className="w-4 h-4 text-primary ml-auto" />}
                    </div>
                 </div>

                 <div 
                   className={cn(
                     "flex flex-col gap-2 p-4 rounded-xl border-2 cursor-pointer transition-all hover:bg-accent/5",
                     recordingMode === "start_stop" 
                       ? "border-primary bg-primary/5" 
                       : "border-border/60 hover:border-primary/50"
                    )}
                    onClick={() => handleRecordingModeChange("start_stop")}
                  >
                    <div className="flex items-center gap-3">
                      <div className={cn(
                        "p-2 rounded-lg",
                        recordingMode === "start_stop" ? "bg-primary/10 text-primary" : "bg-secondary text-muted-foreground"
                      )}>
                        <ToggleLeft className="w-5 h-5" />
                      </div>
                      <div className="space-y-1">
                        <p className="font-medium text-sm leading-none">Start / Stop</p>
                        <p className="text-xs text-muted-foreground">Press once to start, again to stop</p>
                      </div>
                      {recordingMode === "start_stop" && <Check className="w-4 h-4 text-primary ml-auto" />}
                    </div>
                 </div>
               </div>
            </div>

            <Separator />

            <div className="space-y-3">
               <div className="space-y-0.5">
                 <Label className="text-base">Custom Vocabulary</Label>
                 <p className="text-sm text-muted-foreground">Add specific words or phrases to improve accuracy (comma separated)</p>
               </div>
                <Textarea 
                  value={customVocabulary}
                  onChange={(e) => setCustomVocabulary(e.target.value)}
                  onBlur={handleCustomVocabBlur}
                  placeholder="e.g. Replit, TypeScript, OpenAI, specific product names..."
                  className="min-h-[80px] font-mono text-sm resize-none bg-secondary/20"
                />
            </div>

          </div>
        </section>

        {/* API Keys */}
        <section className="space-y-4">
          <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-4">API Configuration</h2>
          <div className="bg-card border border-border/60 rounded-xl p-6 space-y-4">
            <div className="space-y-2">
               <Label>OpenAI API Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showOpenAIKey ? "text" : "password"} 
                     value={openAIKey} 
                     onChange={(e) => setOpenAIKey(e.target.value)}
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowOpenAIKey(!showOpenAIKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showOpenAIKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['OpenAI'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('OpenAI')}
                   className={savedKeys['OpenAI'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['OpenAI'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['OpenAI'] ? "Saved" : "Save"}
                 </Button>
               </div>
               <p className="text-xs text-muted-foreground">Used for summarization and advanced analysis</p>
            </div>
            
            <div className="space-y-2 pt-2">
               <Label>Deepgram API Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showDeepgramKey ? "text" : "password"} 
                     value={deepgramKey} 
                     onChange={(e) => setDeepgramKey(e.target.value)}
                     placeholder="Enter your Deepgram key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowDeepgramKey(!showDeepgramKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showDeepgramKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['Deepgram'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('Deepgram')}
                   className={savedKeys['Deepgram'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['Deepgram'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['Deepgram'] ? "Saved" : "Save"}
                 </Button>
               </div>
            </div>

            <div className="space-y-2 pt-2">
               <Label>AssemblyAI API Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showAssemblyAIKey ? "text" : "password"} 
                     value={assemblyAIKey} 
                     onChange={(e) => setAssemblyAIKey(e.target.value)}
                     placeholder="Enter your AssemblyAI key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowAssemblyAIKey(!showAssemblyAIKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showAssemblyAIKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['AssemblyAI'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('AssemblyAI')}
                   className={savedKeys['AssemblyAI'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['AssemblyAI'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['AssemblyAI'] ? "Saved" : "Save"}
                 </Button>
               </div>
            </div>

            <div className="space-y-2 pt-2">
               <Label>Gemini API Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showGeminiKey ? "text" : "password"} 
                     value={geminiKey} 
                     onChange={(e) => setGeminiKey(e.target.value)}
                     placeholder="Enter your Gemini key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowGeminiKey(!showGeminiKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showGeminiKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['Gemini'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('Gemini')}
                   className={savedKeys['Gemini'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['Gemini'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['Gemini'] ? "Saved" : "Save"}
                 </Button>
                </div>
            </div>

            <div className="space-y-2 pt-2">
               <Label>YouTube API Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input
                     type={showYoutubeKey ? "text" : "password"}
                     value={youtubeKey}
                     onChange={(e) => setYoutubeKey(e.target.value)}
                     placeholder="Enter your YouTube key"
                     className="font-mono text-sm pr-10"
                   />
                   <button
                     type="button"
                     onClick={() => setShowYoutubeKey(!showYoutubeKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showYoutubeKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button
                   variant={savedKeys["YouTube"] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey("YouTube")}
                   className={savedKeys["YouTube"] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys["YouTube"] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys["YouTube"] ? "Saved" : "Save"}
                 </Button>
               </div>
               <p className="text-xs text-muted-foreground">Used for the Youtube tab (search / metadata).</p>
            </div>

            <div className="space-y-2 pt-2">
               <Label>Soniox API Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showSonioxKey ? "text" : "password"} 
                     value={sonioxKey} 
                     onChange={(e) => setSonioxKey(e.target.value)}
                     placeholder="Enter your Soniox key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowSonioxKey(!showSonioxKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showSonioxKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['Soniox'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('Soniox')}
                   className={savedKeys['Soniox'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['Soniox'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['Soniox'] ? "Saved" : "Save"}
                 </Button>
               </div>
            </div>

            <div className="space-y-2 pt-2">
               <Label>ElevenLabs API Key (via Fal.ai)</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showElevenLabsKey ? "text" : "password"} 
                     value={elevenLabsKey} 
                     onChange={(e) => setElevenLabsKey(e.target.value)}
                     placeholder="Enter your Fal.ai key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowElevenLabsKey(!showElevenLabsKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showElevenLabsKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['ElevenLabs'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('ElevenLabs')}
                   className={savedKeys['ElevenLabs'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['ElevenLabs'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['ElevenLabs'] ? "Saved" : "Save"}
                 </Button>
               </div>
            </div>

            <div className="space-y-2 pt-2">
               <Label>Azure Speech Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showAzureKey ? "text" : "password"} 
                     value={azureKey} 
                     onChange={(e) => setAzureKey(e.target.value)}
                     placeholder="Enter your Azure key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowAzureKey(!showAzureKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showAzureKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['Azure'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('Azure')}
                   className={savedKeys['Azure'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['Azure'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['Azure'] ? "Saved" : "Save"}
                  </Button>
                </div>
            </div>

            <div className="space-y-2 pt-2">
               <Label>Azure Speech Region</Label>
               <Input
                 value={azureRegion}
                 onChange={(e) => setAzureRegion(e.target.value)}
                 placeholder="e.g. westus"
                 className="font-mono text-sm"
               />
               <p className="text-xs text-muted-foreground">Required for Azure Speech STT.</p>
            </div>

            <div className="space-y-2 pt-2">
               <Label>Gladia API Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showGladiaKey ? "text" : "password"} 
                     value={gladiaKey} 
                     onChange={(e) => setGladiaKey(e.target.value)}
                     placeholder="Enter your Gladia key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowGladiaKey(!showGladiaKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showGladiaKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['Gladia'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('Gladia')}
                   className={savedKeys['Gladia'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['Gladia'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['Gladia'] ? "Saved" : "Save"}
                 </Button>
               </div>
            </div>

            <div className="space-y-2 pt-2">
               <Label>Groq API Key</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showGroqKey ? "text" : "password"} 
                     value={groqKey} 
                     onChange={(e) => setGroqKey(e.target.value)}
                     placeholder="Enter your Groq key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowGroqKey(!showGroqKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showGroqKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['Groq'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('Groq')}
                   className={savedKeys['Groq'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['Groq'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['Groq'] ? "Saved" : "Save"}
                 </Button>
               </div>
            </div>

            <div className="space-y-2 pt-2">
               <Label>AWS Access Key ID (Transcribe)</Label>
               <div className="flex gap-2">
                 <div className="relative flex-1">
                   <Input 
                     type={showAwsKey ? "text" : "password"} 
                     value={awsKey} 
                     onChange={(e) => setAwsKey(e.target.value)}
                     placeholder="Enter your AWS Access Key" 
                     className="font-mono text-sm pr-10" 
                   />
                   <button 
                     type="button"
                     onClick={() => setShowAwsKey(!showAwsKey)}
                     className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                   >
                     {showAwsKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                   </button>
                 </div>
                 <Button 
                   variant={savedKeys['AWS'] ? "default" : "outline"}
                   onClick={() => handleSaveApiKey('AWS')}
                   className={savedKeys['AWS'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                 >
                   {savedKeys['AWS'] ? <Check className="w-4 h-4 mr-2" /> : null}
                   {savedKeys['AWS'] ? "Saved" : "Save"}
                 </Button>
               </div>
            </div>
          </div>
        </section>

      </div>
    </div>
  );
}
