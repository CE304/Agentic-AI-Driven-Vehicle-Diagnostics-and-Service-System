# OBD Acquisition Skill

- Acquire DTC, PID and operating-condition data from the local OBD service.
- Preserve units, timestamps, source and simulated/live flags.
- The same complete OBD snapshot must be used as the common case evidence for all consulted experts.
- Missing PIDs must be marked unavailable; never fabricate sensor values.
- Read-only diagnosis is the default. Clearing DTC requires an explicit user action in the web UI.
