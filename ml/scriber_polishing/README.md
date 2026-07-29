# Scriber Polishing

This isolated Python 3.12 project builds, validates, trains, evaluates, and
publishes the local `google/gemma-3-270m-it` Scriber transcript-polishing
engine. It does not modify Scriber's production Python environment.

All training and reference data is synthetic and AI-generated. Acceptance is
performed by independent AI critics plus deterministic validators; there is no
human curation or human gold set.

The release pipeline is fail-closed. Generated corpora, checkpoints, model
weights, caches, and credentials are never committed.
