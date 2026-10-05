# Deployment Architecture

| Service | Typical port | Purpose |
|---|---:|---|
| Web UI | 5000 | Case input, diagnostic progress, work order, repair guidance |
| n8n | 5678 | Agentic workflow orchestration |
| OBD API | 8768 | Live DTC/PID/operating-condition data |
| Vehicle History API | 8770 | Read-only SQL evidence search |
| MCP Server | 8000 | RAG/OBD/system tools for n8n AI Agent |
| Local LLM server | 1234 | OpenAI-compatible local Gemma inference |
| 3D preview (optional) | 8765 | Interactive component visualization |

Recommended deployment binds internal services to loopback. Only the user-facing web service should be exposed to the LAN when required, with appropriate firewall and authentication controls.
