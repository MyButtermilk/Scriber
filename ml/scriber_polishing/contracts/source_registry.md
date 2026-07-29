# Quellenregister: Gemma-Polishing

Stand: 2026-07-29. Dieses Register enthält nur Primärquellen bzw. die
offiziellen Dokumentationen ihrer Herausgeber. Es ist ein technischer
Nachweis, keine Rechtsberatung; bei Veröffentlichung ist der dann geltende
Originaltext der Gemma Terms maßgeblich.

## Basismodell und Weitergabe

| Gegenstand | Verifizierter Fakt | Primärquelle |
| --- | --- | --- |
| Basismodell | `google/gemma-3-270m-it` ist das offizielle Google-Repository, als `gemma` lizenziert; der Dateizugriff verlangt die Annahme der Google-Nutzungslizenz. | [Modellkarte und Usage](https://huggingface.co/google/gemma-3-270m-it) |
| Kapazität | Für Gemma 3 270M gelten maximal 32K Eingabe- und bis zu 32K Ausgabetokens pro Anfrage (Ausgabe abzüglich Eingabe); das Modell wurde mit 6 Bio. Tokens trainiert, Wissensstichtag August 2024. | [Google-Modellkarte](https://ai.google.dev/gemma/docs/core/model_card_3) |
| Prompting | IT-Modelle verwenden die kontrollierten Turns `<start_of_turn>user`, `<end_of_turn>` und den Generierungspräfix `<start_of_turn>model`; ein `system`-Turn ist nicht unterstützt. In Transformers daher die mitgelieferte `apply_chat_template(..., add_generation_prompt=True)` verwenden, kein geratenes Eigenformat. | [Google Prompt-Format](https://ai.google.dev/gemma/docs/core/prompt-structure), [offizielles HF-Beispiel](https://huggingface.co/google/gemma-3-270m-it) |
| Distribution | Gemma 3 steht im Anhang der Terms. Nicht-gehostete Weitergabe von Gemma oder Model Derivatives verlangt u. a. Agreement-Kopie, durchsetzbare Weitergabe der Use Restrictions, deutliche Änderungsvermerke und eine `NOTICE` mit dem exakt vorgegebenen Hinweis auf die Terms. Zusätzliche Bedingungen dürfen nicht kollidieren; die Prohibited Use Policy ist einbezogen. | [Gemma Terms, §§ 3.1–3.2 und Anhang](https://ai.google.dev/gemma/terms), [Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy) |
| Unsicherheit | Ob ein konkretes Fine-Tune, Adapter, GGUF oder eine Vertriebsform im Einzelfall ein „Model Derivative“ bzw. eine „Distribution“ ist, wird hier nicht ausgelegt. Die Release-Pipeline muss Terms/Policy vor jedem Upload erneut prüfen und die Rechtsprüfung einbeziehen. | [Gemma Terms: Definitionen und Distribution](https://ai.google.dev/gemma/terms) |

## Offizielle ML-Dokumentation

| Bereich | Verifizierter Einsatzpunkt | Primärquelle |
| --- | --- | --- |
| Transformers | Für Chat-Daten ist die tokenizer-eigene Chatvorlage maßgeblich; Quantisierung via BitsAndBytes-Konfiguration ist dokumentiert. GGUF wird als Import-/Ladeformat dokumentiert; eine Gemma-3-GGUF-Exportzusage dieser Quellen wurde nicht festgestellt. | [Chat templates](https://huggingface.co/docs/transformers/main/chat_templating), [Bitsandbytes](https://huggingface.co/docs/transformers/main/quantization/bitsandbytes), [GGUF](https://huggingface.co/docs/transformers/main/gguf) |
| TRL | `SFTTrainer` unterstützt Supervised Fine-Tuning für sprachmodellierte und konversationelle Datensätze sowie PEFT-Konfigurationen; die tatsächliche Paketversion ist im Lockfile festzuhalten. | [SFT Trainer](https://huggingface.co/docs/trl/main/sft_trainer) |
| PEFT | LoRA wird über `LoraConfig`/PEFT integriert; `merge_and_unload()` erzeugt aus Basis plus Adapter ein eigenständiges Inferenzmodell. | [LoRA-Referenz](https://huggingface.co/docs/peft/main/package_reference/lora) |
| Accelerate | `accelerate config` bzw. `write_basic_config` und `accelerate launch` konfigurieren/starten GPU-Training; `bf16` ist laut Dokumentation ab NVIDIA Ampere mit PyTorch >= 1.10 verfügbar. | [Installation](https://huggingface.co/docs/accelerate/main/basic_tutorials/install), [Launch](https://huggingface.co/docs/accelerate/main/basic_tutorials/launch) |
| Datasets | `Dataset`/`DatasetDict.push_to_hub` veröffentlicht Parquet-Splits; `private=True`, `revision` und `create_pr` sind dokumentierte Upload-Steuerungen. | [Dataset API](https://huggingface.co/docs/datasets/main/package_reference/main_classes) |
| Hugging Face Hub | `create_repo(..., private=True)` erstellt private Repositories; `HfApi.upload_folder` kann zusammengehörige Artefakte in einem Commit hochladen. Revision/Commit-ID danach erfassen und frisch herunterladen. | [Repositories](https://huggingface.co/docs/huggingface_hub/main/guides/repository), [Upload](https://huggingface.co/docs/huggingface_hub/main/guides/upload) |
| PyTorch/CUDA unter Windows | Die offizielle Installation wird über den auf Plattform, CUDA und Paketmanager abgestimmten Selector bereitgestellt. Vor Training muss `torch.cuda.is_available()` auf dem Zielgerät geprüft und gemessen werden. Keine feste CUDA- oder VRAM-Annahme aus dieser Quelle ableiten. | [PyTorch Start Locally](https://pytorch.org/get-started/locally/), [CUDA-Verfügbarkeit](https://pytorch.org/docs/stable/notes/cuda.html) |

Die Bibliotheksangaben wurden zum Stand oben zusätzlich gegen die offiziellen
Dokumentationsbestände von Context7 abgefragt. Versionen, Windows-Support und
GGUF-Exportfähigkeit bleiben releaseabhängig und sind im reproduzierbaren
Environment-Lock sowie durch lokale Smoke-Tests zu belegen.

## Deutsche Referenzen

| Norm / Regelwerk | Verifizierter Einsatzpunkt | Amtliche Quelle |
| --- | --- | --- |
| Einkommensteuergesetz (`EStG`) | Gesetzesabkürzung und Zitiergrundlage für den im Ziel genannten Fall `§ 7 Absatz 4 Satz 2 EStG`; keine inhaltliche Umdeutung durch den Polisher. | [EStG im Bundesrecht](https://www.gesetze-im-internet.de/estg/BJNR010050934.html) |
| Körperschaftsteuergesetz (`KStG`) | Gesetzesabkürzung und Zitiergrundlage für `§ 8c Absatz 1 Satz 1 KStG` sowie `§§ 8c und 8d KStG`. | [KStG im Bundesrecht](https://www.gesetze-im-internet.de/kstg_1977/BJNR004130977.html) |
| Gewerbesteuergesetz (`GewStG`) | Gesetzesabkürzung und Zitiergrundlage für `§ 8 Nummer 1 Buchstabe a GewStG`. | [GewStG im Bundesrecht](https://www.gesetze-im-internet.de/gewstg/BJNR004840934.html) |
| Grundgesetz (`GG`) | Gesetzesabkürzung und Zitiergrundlage für `Artikel 3 Absatz 1 GG`. | [GG im Bundesrecht](https://www.gesetze-im-internet.de/gg/BJNR000010949.html) |
| Orthografie | Das Amtliche Regelwerk 2024 einschließlich Wörterverzeichnis ist seit 01.07.2024 für Schule und Verwaltung verbindlich. Es ist die normative Referenz; Varianten außerhalb eindeutig regelbarer Fälle nicht aggressiv normalisieren. | [Amtliches Regelwerk 2024 (PDF)](https://www.rechtschreibrat.com/DOX/RfdR_Amtliches-Regelwerk_2024.pdf), [amtliche Mitteilung](https://www.rechtschreibrat.com/DOX/RfdR_PM_2024-07-03_Aktualisierung_Regelwerk.pdf) |

`gesetze-im-internet.de` wird vom Bundesamt für Justiz im Auftrag des
Bundesministeriums der Justiz bereitgestellt. Die Pipeline darf diese Quellen
nur zur Canonicalisierung ausdrücklich erkannter Zitate verwenden: Sie darf
niemals Normnummern, Absätze, Sätze, Nummern oder Buchstaben ergänzen,
ändern oder aus Textkontext erraten.
