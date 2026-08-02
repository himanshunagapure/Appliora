import json

import pytest

from llm.client import LLMClient


def test_parse_json_strips_markdown_fence():
    raw = '```json\n{"name": "Ada", "skills": ["Python"]}\n```'
    assert LLMClient._parse_json(raw) == {"name": "Ada", "skills": ["Python"]}


def test_parse_json_fixes_trailing_commas():
    raw = '{\n  "skills": ["Python", "SQL",],\n  "education": [{"degree": "B.E.",}],\n}'
    assert LLMClient._parse_json(raw) == {
        "skills": ["Python", "SQL"],
        "education": [{"degree": "B.E."}],
    }


def test_parse_json_repairs_missing_commas():
    # Gemini's recurring failure: drop the comma after a closed array/object.
    raw = '{\n  "skills": ["Python", "SQL"]\n  "experience": []\n}'
    assert LLMClient._parse_json(raw) == {
        "skills": ["Python", "SQL"],
        "experience": [],
    }


def test_parse_json_extracts_object_from_prose():
    raw = 'Here is the profile:\n{"contact": {"name": "Ada"}}\nThanks!'
    assert LLMClient._parse_json(raw) == {"contact": {"name": "Ada"}}


def test_parse_json_rejects_empty():
    with pytest.raises(json.JSONDecodeError):
        LLMClient._parse_json("   ")
