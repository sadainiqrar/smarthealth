# SmartHealth — Part B: GenAI Layer

> Text extraction of `SmartHealth - Part B.docx` (the .docx is the authoritative source).

## Context

Same MediNova / SmartHealth context as [Part A](part-a-core-platform.md). MediNova now wants
to evolve SmartHealth into an **intelligent healthcare platform powered by GenAI**.

## Problem Statement

Solve patient engagement and operational efficiency challenges through GenAI. Current issues:

- Patients struggle to find the right services or specialties
- Appointment journeys are static and non-personalized
- Staff spend time answering repetitive queries
- Clinical and operational data is underused for insights
- Follow-up communication is mostly manual

This phase introduces: AI healthcare assistant, intelligent appointment support, automated
patient communication generation, semantic knowledge retrieval, streaming AI responses.

## Business Goals & Vision

### Intelligent patient experience

Patients should be able to ask questions such as:

- "Which specialist should I consult for my symptoms?"
- "What preparation is needed before my test?"
- "Show available appointments this week"
- "Explain my appointment steps"

The platform should provide contextual and helpful responses.

### Operational productivity enhancement

Teams should be able to generate: appointment summaries, follow-up communication drafts, FAQ
responses, operational performance summaries, patient engagement campaigns.

### Smooth real-time interactions

Long-running AI responses should support streaming or asynchronous delivery and **must not
block core healthcare operations**.

## Core Functional Requirements

### Content / Data Preparation Workflow

When healthcare operational data is updated: relevant data chunked, embeddings generated,
searchable vector records stored, data made available for semantic retrieval.

### Intelligent Healthcare Assistant

**A. Contextual Patient Q&A** — retrieve relevant service, provider, and operational
information; use contextual understanding; generate meaningful responses; handle incomplete or
vague queries.

**B. Engagement & Recommendation Support** — generate appointment reminders, follow-up
guidance, service recommendations, preventive care suggestions, operational assistance
responses.

**C. Report & Summary Generation** — generate daily appointment summaries, department
utilization reports, patient engagement summaries, executive operational snapshots. Responses
may be long and should support incremental delivery.

## Analytics Metrics

- AI Assistant Usage
- Questions Asked / Answered
- Booking Conversion After AI Interaction
- Generated Communication Usage
- Average AI Response Time
- Engagement Improvement Metrics

## Observability

Ability to diagnose failures in: AI assistant interactions, retrieval pipeline, streaming
responses, external LLM provider calls.

## Expected Outcomes

- Intelligent healthcare assistant
- Better patient engagement
- Context-aware appointment guidance
- Automated communication workflows
- Scalable AI processing layer

## PRD Requirement

- AI use-cases
- Functional / non-functional requirements
- Delivery milestones
- Feature traceability

## Tech Stack

- LangGraph / LangChain
- LLM Provider (OpenAI / Groq / Anthropic)
- Vector DB
