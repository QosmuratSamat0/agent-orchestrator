from __future__ import annotations

import logging
import re
import subprocess
from typing import Dict, List, Optional, Set, Tuple

from agent_orchestrator.config import ModelRoutingConfig
from agent_orchestrator.models import ModelSelection, TaskComplexity

logger = logging.getLogger("agent_orchestrator.model_router")

# Authoritative models verified from live `agy models` output
VERIFIED_AGY_MODELS: Set[str] = {
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
}


class ModelRouter:
    """Selects the optimal Antigravity model and effort level based on task complexity, risk, and verified models."""

    def __init__(
        self,
        config: Optional[ModelRoutingConfig] = None,
        agy_bin: str = r"C:\Users\admin\AppData\Local\agy\bin\agy.exe",
    ):
        self.config = config or ModelRoutingConfig()
        self.agy_bin = agy_bin
        self._available_models: Set[str] = set(VERIFIED_AGY_MODELS)
        self._load_available_models_from_cli()

    def _load_available_models_from_cli(self) -> None:
        """Dynamically query `agy models` to refresh verified models."""
        try:
            res = subprocess.run(
                [self.agy_bin, "models"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0:
                models = set()
                for line in res.stdout.splitlines():
                    parts = line.strip().split()
                    if parts and not parts[0].startswith("Fetching") and not parts[0].startswith("Usage"):
                        models.add(parts[0])
                if models:
                    self._available_models = models
                    logger.debug(f"Loaded {len(models)} models from agy models CLI")
        except Exception as e:
            logger.debug(f"Using static verified models list (agy models query failed: {e})")

    @property
    def available_models(self) -> Set[str]:
        return set(self._available_models)

    def classify_complexity(self, prompt: str, acceptance_criteria: str = "") -> Tuple[TaskComplexity, str]:
        """Deterministic rule-based complexity classifier evaluating size, ambiguity, and risk."""
        full_text = f"{prompt} {acceptance_criteria}".lower()
        word_count = len(full_text.split())

        # Keywords indicative of expert / system-level ambiguity
        expert_keywords = [
            "архитектура ядра", "kernel", "миграция всей базы", "distributed consensus",
            "критическая уязвимость", "zero-day", "system-level", "race condition in multiprocessing",
            "консенсус", "полная переработка архитектуры", "неопределенность", "высокий риск"
        ]
        # Keywords indicative of complex tasks
        complex_keywords = [
            "рефакторинг", "refactor", "отладка", "debugging", "архитектур", "оптимизация производительности",
            "deadlock", "memory leak", "memory-leak", "multithreading", "сложная логика", "state machine",
            "исследование", "несколько модулей", "компонентов", "asyncio lifecycle"
        ]
        # Keywords indicative of moderate tasks
        moderate_keywords = [
            "тесты", "pytest", "unit test", "новый модуль", "компонент", "api эндпоинт",
            "интеграция", "валидация", "несколько файлов", "обработка ошибок", "хэндлер"
        ]
        # Keywords indicative of trivial tasks
        trivial_keywords = [
            "опечатка", "typo", "исправь опечатку", "переименуй", "комментарий",
            "docstring", "исправь заголовок", "исправь орфографию", "одно слово"
        ]

        if any(kw in full_text for kw in expert_keywords):
            return TaskComplexity.EXPERT, "Обнаружены признаки системного уровня, высокой неопределённости или критического риска."

        if any(kw in full_text for kw in complex_keywords) or word_count > 300:
            return TaskComplexity.COMPLEX, "Задача требует глубокого анализа, сложной отладки, рефакторинга или принятия архитектурных решений."

        if any(kw in full_text for kw in trivial_keywords) and word_count < 40:
            return TaskComplexity.TRIVIAL, "Простое исправление опечатки, комментария или точечная правка пары слов."

        if any(kw in full_text for kw in moderate_keywords) or word_count > 80:
            return TaskComplexity.MODERATE, "Задача затрагивает несколько файлов, логику валидации или написание тестов."

        return TaskComplexity.SIMPLE, "Чётко очерченная локальная правка или функция умеренного объёма."

    def select_model(
        self,
        prompt: str,
        acceptance_criteria: str = "",
        manual_model: Optional[str] = None,
        manual_effort: Optional[str] = None,
    ) -> ModelSelection:
        complexity, reason = self.classify_complexity(prompt, acceptance_criteria)

        # 1. Manual override
        if manual_model:
            model_to_use = manual_model
            is_valid = model_to_use in self._available_models
            if not is_valid:
                logger.warning(
                    f"Manual model '{manual_model}' not found in available models. Falling back."
                )
                model_to_use = self.config.fallback
                return ModelSelection(
                    model=model_to_use,
                    effort=manual_effort or "medium",
                    complexity=complexity,
                    reason=f"Ручной выбор '{manual_model}' недоступен. Применён fallback: {model_to_use}",
                    is_fallback=True,
                    is_override=True,
                )
            return ModelSelection(
                model=model_to_use,
                effort=manual_effort or "medium",
                complexity=complexity,
                reason=f"Ручное переопределение пользователем: {manual_model}",
                is_override=True,
            )

        # 2. Deterministic policy mapping
        target_model: str
        target_effort: str

        if complexity == TaskComplexity.TRIVIAL:
            target_model = self.config.trivial
            target_effort = "low"
        elif complexity == TaskComplexity.SIMPLE:
            target_model = self.config.simple
            target_effort = "low"
        elif complexity == TaskComplexity.MODERATE:
            target_model = self.config.moderate
            target_effort = "medium"
        elif complexity == TaskComplexity.COMPLEX:
            target_model = self.config.complex
            target_effort = "high"
        else:  # EXPERT
            target_model = self.config.expert
            target_effort = "high"

        # 3. Availability check & Fallback
        is_fallback = False
        if target_model not in self._available_models:
            logger.warning(
                f"Selected model '{target_model}' not present in available models list. Activating fallback."
            )
            target_model = self.config.fallback
            is_fallback = True
            reason += f" (Модель по умолчанию недоступна, задействован fallback {target_model})"

        if manual_effort:
            target_effort = manual_effort

        logger.info(
            f"Model selected: {target_model} (effort={target_effort}, complexity={complexity.value}). Reason: {reason}"
        )

        return ModelSelection(
            model=target_model,
            effort=target_effort,
            complexity=complexity,
            reason=reason,
            is_fallback=is_fallback,
        )
