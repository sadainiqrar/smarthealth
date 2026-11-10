# Contracts

The only things services share (Weeks 3-5 spec §1.2): files, never code. Each service
generates or loads its own bindings from here.

| Directory | What | Compatibility rule, checked in CI |
| --- | --- | --- |
| `events/` | One Avro value schema per Kafka topic, named after the topic's logical name (`appointments.cancelled.avsc`). Every schema has the same envelope -- `event_id`, `event_type`, `schema_version`, `occurred_at`, `producer`, `subject_id` -- and a topic-specific `payload` record | `BACKWARD_TRANSITIVE` in Schema Registry: a consumer on the newest schema reads every event ever written, which compacted `*.state` topics need because new consumers replay them from offset 0 |
| `proto/` | gRPC contracts (Part B, from Week 3 slice 7) | `buf breaking` |

Changing a schema: add fields with defaults, never remove or retype one, and bump
`schema_version` in `app/events/catalog.py`. Clinical content never goes in an event
(spec D-31) -- ids and statuses only.
