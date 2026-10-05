# AI Model Disclosure / 開源模型揭露

## Core model

| Item | Disclosure |
|---|---|
| Model | `google/gemma-4-26B-A4B` |
| Developer | Google DeepMind |
| Architecture | Gemma 4, Mixture-of-Experts; 26B total / A4B active class |
| Official source | https://huggingface.co/google/gemma-4-26B-A4B |
| Official model card | https://ai.google.dev/gemma/docs/core/model_card_4 |
| License | Apache-2.0 |
| Project role | Agentic AI supervisor reasoning, expert consultation, evidence synthesis and work-order/report generation |
| Deployment | Local inference through OpenAI-compatible local model server; weights are not stored in this repo |

## Why the model is a core component

The model is not used only to rewrite text. It decides which diagnostic tools and domain experts are needed, interprets structured OBD/PID/DTC evidence, combines RAG/SQL evidence, and produces a structured repair workflow with verification steps. Tool calls and evidence constraints are controlled by n8n workflows and Skill policies.

## Competition restriction statement

The public competition version is designed around the Google DeepMind Gemma 4 model family. It does not include DeepSeek or another China-origin model as the core model. Any local deployment used for judging should keep the same disclosed model identity and record the exact local file/quantization in the team's deployment log.

## Reproducibility note

Model weights are intentionally omitted from GitHub because they are large. Download the model from the official source above and follow its Apache-2.0 license. If a quantized derivative is used, document the derivative repository, quantization format and commit/revision before the final live demonstration.
