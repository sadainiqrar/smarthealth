"""The event catalog and the contract files stay in step, and every schema keeps the
one envelope consumers rely on.

Two lists of the same thing -- topics in app/events/catalog.py, schemas in
contracts/events/ -- drift unless something fails when they do. A topic with no schema
fails at its first publish; a schema with no topic is a contract nobody honours.
"""

import json

import pytest

from app.events import catalog, schemas

pytestmark = pytest.mark.meta

ENVELOPE = ["event_id", "event_type", "schema_version", "occurred_at", "producer", "subject_id",
            "payload"]


def test_every_topic_has_a_schema_and_every_schema_a_topic():
    assert set(catalog.TOPICS) == schemas.schema_names()


@pytest.mark.parametrize("name", sorted(catalog.TOPICS))
def test_every_schema_parses_and_carries_the_envelope(name):
    schema = json.loads(schemas.schema_text(name))
    schemas.parsed_schema(name)
    assert [field["name"] for field in schema["fields"]] == ENVELOPE
    assert schema["namespace"] == "smarthealth.events.v1"


@pytest.mark.parametrize("name", sorted(catalog.TOPICS))
def test_every_optional_field_has_a_default(name):
    """A field without a default cannot be added later without breaking old readers --
    and a nullable one without a default is the usual way that mistake starts."""
    payload = json.loads(schemas.schema_text(name))["fields"][-1]["type"]
    for field in payload["fields"]:
        if isinstance(field["type"], list):
            assert "default" in field, f"{name}.{field['name']} is nullable with no default"


@pytest.mark.parametrize("name", sorted(catalog.TOPICS))
def test_no_event_carries_clinical_content(name):
    """Spec D-31: events carry ids and statuses; clinical text is read through Part A's
    API under its authorisation. A field with one of these names is a leak in waiting."""
    forbidden = {"diagnosis", "diagnoses", "prescription", "prescriptions", "notes", "findings",
                 "complaint", "result", "results"}
    payload = json.loads(schemas.schema_text(name))["fields"][-1]["type"]
    assert not {field["name"] for field in payload["fields"]} & forbidden


def test_state_topics_are_compacted_and_event_topics_are_not():
    for entry in catalog.TOPICS.values():
        policy = catalog.topic_config(entry)["cleanup.policy"]
        expected = "compact" if entry.name.endswith(".state") else "delete"
        assert policy == expected, entry.name
