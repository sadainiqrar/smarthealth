# SmartHealth — Execution Guidelines

> Text extraction of `SmartHealth - Guidelines.docx` (the .docx is the authoritative source).

## 1. Objective

A structured learning journey for engineers. The intention is not only to build features, but
to understand distributed healthcare operations systems, learn event-driven architecture
patterns, design for scalability and reliability, gain workflow orchestration experience,
apply GenAI in service platforms, and improve engineering system thinking.

## 2. Execution Approach

**Module-based progression** — complete each module in sequence, share progress with mentor
regularly, incorporate feedback before the next stage, build strong foundations before
advanced modules.

**Weekly milestone flow** — work on assigned scope → submit implementation for review →
discuss technical decisions → improve based on feedback → continue to next milestone.

**Mentor collaboration** — explain architecture choices, discuss tradeoffs, highlight blockers,
ask questions openly. Reviews focus on system thinking, design clarity, tool understanding,
and implementation quality.

## 3. Submission Guidelines

**GitHub repository:** clean project structure, logical module separation, meaningful commits,
good code organization.

**README:** project overview, architecture diagram, setup instructions, API overview,
technology stack used.

**Technical documentation:** service/module breakdown, data flows, event flows, key design
decisions, assumptions and tradeoffs.

## 4. Learning Expectations

**Part A (Core Platform):** SW engineering fundamentals, AuthZ & AuthN, data storage,
event-driven architecture, idempotency, operational consistency, workflow orchestration, async
systems, observability basics.

**Part B (GenAI Layer):** data parsing / ingestion / indexing, embeddings and vector search,
retrieval-augmented systems, evaluations, prompt engineering, response quality, streaming and
latency handling.

## 5. Part A — Execution Plan (3 Weeks)

Focus: Healthcare Core + Distributed Systems

### Week 1 — Foundation + Core Services

**Scope:** initial architecture and project setup; patient and provider management APIs; user
roles and auth basics; appointment schema design; local environment setup.

**Deliverables:** working APIs; database schema ready; Docker setup operational; clean service
structure established.

### Week 2 — Scheduling + Service Workflow

**Scope:** appointment scheduling workflow using Temporal; slot reservation logic; visit
lifecycle handling; duplicate prevention; state transitions and validations.

**Deliverables:** stable scheduling workflow; provider orchestration ready; reliable
appointment state machine.

### Week 3 — Event-Driven System + Observability

**Scope:** Kafka integration; Celery background workers; notification flows; basic analytics
metrics; logs, tracing, dashboards.

**Deliverables:** event-driven workflows operational; monitoring dashboards ready; distributed
tracing enabled.

### Part A — Weekly Tracking Table

| Week | Focus Area | What to Aim For | Done |
| --- | --- | --- | --- |
| Week 1 | Foundation & Core Services | Working APIs, schema, setup | |
| Week 2 | Scheduling & Workflows | Stable booking + visit workflows | |
| Week 3 | Events & Observability | Async processing + operational visibility | |

## 6. Part B — Execution Plan (2 Weeks)

Focus: GenAI + Intelligent Healthcare Layer

### Week 4 — Data Preparation + Retrieval

**Scope:** data parsing (PDF); operational data chunking; embedding generation; vector database
integration; semantic retrieval system.

**Deliverables:** searchable healthcare embeddings; relevant retrieval pipeline working.

### Week 5 — AI Assistant + Streaming

**Scope:** patient AI assistant; booking guidance engine; communication generation engine;
streaming responses; AI monitoring metrics.

**Deliverables:** functional AI assistant; context-aware responses; smooth engagement
experience.

### Part B — Weekly Tracking Table

| Week | Focus Area | What to Aim For | Done |
| --- | --- | --- | --- |
| Week 4 | Ingestion & Retrieval Layer | Indexed operational data + semantic retrieval | |
| Week 5 | AI Healthcare Layer | Assistant + engagement tools | |

## 7. Final Deliverables Checklist

- All milestones completed and reviewed
- Codebase is clean and maintainable
- README and documentation completed
- Key workflows tested end-to-end
- Architecture decisions clearly explainable

## 8. Evaluation Approach

- Architecture clarity
- Code quality and maintainability
- Correct use of defined tech stack
- Reliability and failure handling
- Observability maturity
- Depth of engineering understanding
