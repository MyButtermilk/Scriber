# Scriber Blindvergleich V2 gegen V1 – Terra v1

Bewerte jeden anonymen Fall ausschließlich anhand von Rohtext, Referenz und den Ausgaben A/B. Bevorzuge die Ausgabe, die den Inhalt vollständig und unverändert bewahrt und Rechtschreibung, Grammatik, Zeichensetzung sowie Struktur besser bereinigt. Markiere A und/oder B als kritisch, wenn Fakten, Zahlen, Namen, Bedingungen, Verneinungen oder geschützte Inhalte verändert, ergänzt oder ausgelassen werden.

Gib für jeden Fall genau ein JSON-Objekt mit `schema_version`, `case_id`, `model_family`, `judge_session_id`, `prompt_sha256`, `winner` (`A`, `B` oder `tie`), `critical_candidates` (Teilmenge aus `A`, `B`) und einer kurzen `brief_reason` zurück. Nutze ausschließlich `gpt-5.6-terra` und dieselbe Session für alle 100 Fälle.
