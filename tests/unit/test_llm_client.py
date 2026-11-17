import json

import pytest

from gates.common.llm_client import OpenAICompatibleClient, extract_text, make_client, parse_chat_response


def test_extract_plain_string_content():
    assert extract_text({"choices": [{"message": {"content": "hi"}}]}) == "hi"


def test_extract_skips_reasoning_parts():
    body = {"choices": [{"message": {"content": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking..."}]},
        {"type": "text", "text": '{"a": 1}'},
    ]}}]}
    assert extract_text(body) == '{"a": 1}'


def test_parse_usage():
    r = parse_chat_response({"model": "m", "choices": [{"message": {"content": "x"}}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}, "fallback")
    assert (r.text, r.prompt_tokens, r.completion_tokens, r.total_tokens, r.model) == ("x", 10, 5, 15, "m")


def test_parse_missing_usage_is_zero():
    r = parse_chat_response({"choices": [{"message": {"content": "x"}}]}, "fallback")
    assert r.total_tokens == 0 and r.model == "fallback"


def test_openai_client_posts_chat_completions_at_temperature_zero():
    seen = {}

    def transport(url, headers, body):
        seen.update(url=url, headers=headers, body=json.loads(body))
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    c = OpenAICompatibleClient("https://llm.example.com/v1/", "k", "some-model", transport=transport)
    r = c.chat([{"role": "user", "content": "hi"}], temperature=0.0, max_tokens=50)
    assert r.text == "ok"
    assert seen["url"] == "https://llm.example.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer k"
    assert seen["body"]["temperature"] == 0.0 and seen["body"]["model"] == "some-model"


def test_make_client_validates_provider():
    with pytest.raises(ValueError):
        make_client("carrier-pigeon", endpoint="x")
    with pytest.raises(ValueError):
        make_client("openai", base_url=None, api_key="k", model="m")
    with pytest.raises(ValueError):
        make_client("ai_query", endpoint=None)
