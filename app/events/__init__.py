"""Domain events: the outbox, the relay to Kafka, and the consumer runtime.

Framework-free, like app/core: the relay and the consumers are their own processes and
must never load FastAPI. Modules write events with `app.events.outbox.emit`, in the same
transaction as the change the event describes (Weeks 3-5 spec D-21).
"""
