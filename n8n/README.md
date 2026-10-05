# n8n Workflows

The JSON files are safe-to-publish copies. Credential references were removed, so importing them will **not** expose local credential IDs or account names.

After import, reconnect:
- Local OpenAI-compatible LLM credential (Gemma 4 local server)
- Vehicle History API header credential, if enabled
- Expert sub-workflow selector
- MCP Client endpoint

This intentional sanitization prevents secrets from being published while preserving the workflow nodes, prompts and routing logic for review.
