# Vehicle History DB Skill

- Search the SQL history database before expert reasoning when this skill is enabled.
- Match by domain, DTC, symptom/part keywords and current odometer.
- `data_origin=simulated` must be described as simulated historical data, never as a real repair record.
- Historical similarity is auxiliary evidence and cannot replace current OBD/PID/DTC/RAG evidence.
- If direct evidence is absent, explicitly state that the history database has insufficient direct evidence.
