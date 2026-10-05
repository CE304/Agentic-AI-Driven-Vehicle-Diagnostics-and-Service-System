# 2026 InnoServe AI-Open Source Competition Compliance

## Official group requirements addressed by this repository

1. **Open AI model**: core model is disclosed as Google DeepMind `google/gemma-4-26B-A4B`, with official source and Apache-2.0 license.
2. **GitHub / open platform publication**: source code, n8n workflows, Skill policies, database schema, RAG builder and documentation are prepared for direct GitHub publication. Model source is linked to Hugging Face.
3. **Digital divide**: the prototype targets the capability/information gap in vehicle diagnostics by converting fragmented DTC/PID/manual/history evidence into guided diagnostic tasks for technicians with different experience levels.
4. **Core model role**: the model plans tool use, routes experts, synthesizes evidence and generates a repair work order; it is not merely a text generator.
5. **Usable service / Prototype**: real OBD-II acquisition, RAG/SQL retrieval, multi-expert consultation, web work order and repair-guidance interfaces form a working prototype.
6. **Model source disclosure**: `MODEL_DISCLOSURE.md` records name, developer, source and license.
7. **China-origin restriction**: the public competition release is based on the Google DeepMind Gemma 4 model family and intentionally excludes China-origin core AI models and related proprietary inference services.
8. **System/document completeness**: README, installation, structure, data governance, security, license and third-party notices are included.

## Evaluation mapping

- Innovation: Agentic multi-expert vehicle diagnosis rather than passive DTC lookup.
- Technical maturity: OBD + RAG + SQL + MCP + n8n + local open model integration.
- Social impact: reduces information/skill barriers in maintenance diagnosis and supports training/SME repair environments.
- Documentation: repository structure and evidence are explicitly documented.
