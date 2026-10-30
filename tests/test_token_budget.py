"""每日 token 预算（评审 4.5）：按天入账、超限拒绝、不限额旁路、调用后自动记账。"""
from types import SimpleNamespace

import pytest

from butler.llm_client import LLMError, OpenAICompatClient
from butler.memory.store import MemoryStore
from butler.token_budget import TokenBudget


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "budget.db")


def test_usage_accumulates_per_day(store):
    assert store.add_tokens_used(300) == 300
    assert store.add_tokens_used(200) == 500
    assert store.tokens_used_today() == 500
    budget = TokenBudget(store, 1000)
    assert budget.status() == {
        "enabled": True, "limit": 1000, "used_today": 500, "remaining": 500,
    }


def test_zero_limit_means_unlimited(store):
    budget = TokenBudget(store, 0)
    store.add_tokens_used(999_999)  # 照常入账
    budget.raise_if_exhausted()  # 不抛：0=不限
    assert budget.status()["enabled"] is False


def test_exhausted_budget_raises_llm_error(store):
    budget = TokenBudget(store, 100)
    store.add_tokens_used(100)
    with pytest.raises(LLMError):
        budget.raise_if_exhausted()


def test_llm_client_refuses_calls_before_any_network(store):
    """预算耗尽时，客户端在发起任何网络请求前就拒绝（无需打桩 SDK）。"""
    budget = TokenBudget(store, 100)
    store.add_tokens_used(100)
    client = OpenAICompatClient(
        base_url="http://127.0.0.1:1", api_key="sk-test", model="m",
        token_budget=budget, max_retries=1,
    )
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])


def test_llm_client_records_usage_after_success(store):
    budget = TokenBudget(store, 1000)

    class _FakeCompletions:
        @staticmethod
        def create(**kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="好的", tool_calls=None),
                )],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

    client = OpenAICompatClient(
        base_url="http://127.0.0.1:1", api_key="sk-test", model="m",
        token_budget=budget, max_retries=1,
    )
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions))
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "好的"
    assert store.tokens_used_today() == 15
    assert budget.status()["used_today"] == 15


def test_budget_crossing_limit_then_next_call_refused(store):
    """已发出的那一次调用无法回收：入账越过预算线不抛错，但下一次调用被拒。"""
    budget = TokenBudget(store, 100)
    budget.add(120)  # 越线入账不抛
    with pytest.raises(LLMError):
        budget.raise_if_exhausted()
