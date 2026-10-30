import asyncio

import pytest

from voice_action.planner import FallbackModel, PlannerError


class _Model:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def _run(primary, fallback):
    return asyncio.run(FallbackModel(primary, fallback).generate([], []))


def test_llm_failure_uses_fallback():
    spare = _Model(result="ok")
    assert _run(_Model(error=PlannerError("LLM API 요청 실패 (HTTP 429)")), spare) == "ok"


def test_primary_success_skips_fallback():
    spare = _Model(result="no")
    assert _run(_Model(result="ok"), spare) == "ok"
    assert spare.calls == 0


def test_non_llm_error_is_raised():
    with pytest.raises(PlannerError):
        _run(_Model(error=PlannerError("도구 오류")), _Model(result="ok"))
