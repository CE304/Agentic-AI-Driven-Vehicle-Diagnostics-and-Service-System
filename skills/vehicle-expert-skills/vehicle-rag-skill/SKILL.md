# Vehicle RAG Skill

- Search by vehicle profile + DTC + key PID evidence + symptoms, not by a vague generic query.
- Prefer service procedures, DTC references, PID interpretation, TSB/recall and documented cases.
- Return source metadata with evidence.
- RAG evidence supports diagnosis but does not override current live OBD evidence.
- If the corpus does not support a claim, state that evidence is insufficient.
