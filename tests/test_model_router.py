from __future__ import annotations

import pytest
from agent_orchestrator.config import ModelRoutingConfig
from agent_orchestrator.models import TaskComplexity
from agent_orchestrator.model_router import ModelRouter, VERIFIED_AGY_MODELS


def test_verified_models_set():
    assert "gemini-3.8-flash-high" in VERIFIED_AGY_MODELS
    assert "gemini-3.8-flash-low" in VERIFIED_AGY_MODELS
    assert "gemini-3.1-pro-high" in VERIFIED_AGY_MODELS
    assert "claude-sonnet-4-6" in VERIFIED_AGY_MODELS


def test_classify_trivial(model_router: ModelRouter):
    complexity, _ = model_router.classify_complexity("исправь опечатку в слове status")
    assert complexity == TaskComplexity.TRIVIAL


def test_classify_simple(model_router: ModelRouter):
    complexity, _ = model_router.classify_complexity("добавь проверку на None для аргумента path")
    assert complexity == TaskComplexity.SIMPLE


def test_classify_moderate(model_router: ModelRouter):
    complexity, _ = model_router.classify_complexity(
        "напиши pytest тесты для нового модуля валидации и проверь несколько файлов"
    )
    assert complexity == TaskComplexity.MODERATE


def test_classify_complex(model_router: ModelRouter):
    complexity, _ = model_router.classify_complexity(
        "требуется глубокий рефакторинг, отладка deadlock и оптимизация производительности в state machine"
    )
    assert complexity == TaskComplexity.COMPLEX


def test_classify_expert(model_router: ModelRouter):
    complexity, _ = model_router.classify_complexity(
        "полная переработка архитектуры ядра системы и distributed consensus с высоким риском"
    )
    assert complexity == TaskComplexity.EXPERT


def test_model_selection_policy(model_router: ModelRouter):
    # Trivial -> flash-low
    sel_triv = model_router.select_model("исправь опечатку")
    assert sel_triv.complexity == TaskComplexity.TRIVIAL
    assert sel_triv.model == "gemini-3.8-flash-low"
    assert sel_triv.effort == "low"

    # Moderate -> flash-high
    sel_mod = model_router.select_model("напиши pytest тесты для эндпоинта")
    assert sel_mod.complexity == TaskComplexity.MODERATE
    assert sel_mod.model == "gemini-3.8-flash-high"

    # Complex -> pro-high
    sel_comp = model_router.select_model("сложный рефакторинг и отладка памяти")
    assert sel_comp.complexity == TaskComplexity.COMPLEX
    assert sel_comp.model == "gemini-3.1-pro-high"
    assert sel_comp.effort == "high"


def test_manual_override(model_router: ModelRouter):
    sel = model_router.select_model(
        prompt="простая задача",
        manual_model="claude-sonnet-4-6",
        manual_effort="high",
    )
    assert sel.is_override is True
    assert sel.model == "claude-sonnet-4-6"
    assert sel.effort == "high"


def test_fallback_on_unverified_model():
    cfg = ModelRoutingConfig(
        trivial="non-existent-model-xyz",
        fallback="gemini-3.8-flash-high",
    )
    router = ModelRouter(config=cfg)
    sel = router.select_model("исправь опечатку")
    assert sel.is_fallback is True
    assert sel.model == "gemini-3.8-flash-high"
    assert "fallback" in sel.reason.lower()


@pytest.mark.parametrize("slug,expected", [
    ("gemini-3.8-flash-high", ("gemini-3.8-flash", "high")),
    ("gpt-oss-120b-medium", ("gpt-oss-120b", "medium")),
    ("gemini-3.1-pro-low", ("gemini-3.1-pro", "low")),
    ("claude-sonnet-4-6", ("claude-sonnet-4-6", None)),
    ("claude-opus-4-6-thinking", ("claude-opus-4-6-thinking", None)),
])
def test_split_model_level(slug, expected):
    from agent_orchestrator.model_router import split_model_level
    assert split_model_level(slug) == expected


def test_policy_effort_comes_from_slug(model_router: ModelRouter):
    sel = model_router.select_model("напиши pytest тесты для эндпоинта")
    assert sel.model == "gemini-3.8-flash-high"
    assert sel.effort == "high"


def test_manual_effort_switches_to_sibling_slug(model_router: ModelRouter):
    sel = model_router.select_model("напиши pytest тесты для эндпоинта", manual_effort="low")
    assert (sel.model, sel.effort) == ("gemini-3.8-flash-low", "low")
    sel = model_router.select_model("x", manual_model="gemini-3.8-flash-high", manual_effort="medium")
    assert (sel.model, sel.effort) == ("gemini-3.8-flash-medium", "medium")


def test_manual_effort_without_sibling_is_ignored(model_router: ModelRouter):
    sel = model_router.select_model("простая задача", manual_model="gemini-3.1-pro-high", manual_effort="medium")
    assert (sel.model, sel.effort) == ("gemini-3.1-pro-high", "high")
    assert "проигнорирован" in sel.reason


def test_unleveled_slug_has_effort_only_when_explicit(model_router: ModelRouter):
    assert model_router.select_model("простая задача", manual_model="claude-sonnet-4-6").effort == ""


def test_dispatch_effort_rule():
    from agent_orchestrator.model_router import dispatch_effort
    assert dispatch_effort("gemini-3.8-flash-high", "medium") is None
    assert dispatch_effort("gemini-3.8-flash-high", "high") is None
    assert dispatch_effort("claude-sonnet-4-6", "high") == "high"
    assert dispatch_effort("claude-sonnet-4-6", "") is None
