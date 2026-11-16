# core

Part A of the assignment -- the modular monolith in `app/`. One image
(`smarthealth-core`), six processes, each its own Deployment: `core-api` (FastAPI, Service
`app`, runs migrations on start as in compose), `core-worker` (Temporal), `core-relay`
(outbox -> Kafka; its `setup` initContainer creates topics and registers schemas),
`core-consumers` (Kafka consumers), `core-celery-worker`, `core-celery-beat`.
