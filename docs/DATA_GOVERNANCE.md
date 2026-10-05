# Data Governance

## Evidence hierarchy

1. Current live vehicle evidence: OBD DTC/PID and operating condition.
2. Technical references: RAG documents, DTC/PID references, service procedures, recalls/TSBs.
3. Vehicle-history SQL records: auxiliary similarity evidence.
4. Model reasoning: must cite/reflect evidence and must not transform assumptions into confirmed faults.

## Simulated data

Rows seeded by `vehicle_history_seed.sql` are demo data and are marked `data_origin=simulated`. UI/agent logic must describe them as simulated cases.

## Privacy

VIN, plate number, owner identity and private service records should not be committed. Use masked/sample data for public demonstration.
