"""Batched, typed Jev decisions for list enrichment and product selection."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class JevError(Exception):
    pass


class JevAdvisor:
    def __init__(self, api_key: str | None = None, *, timeout_s: int = 5):
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY")
        if not self.api_key:
            raise JevError("Defina TYPESAFE_API_KEY para usar Jev")
        self.timeout_s = timeout_s

    def choose_many(self, entries: list[dict]) -> dict[str, dict]:
        """Answer multiple independent Choice questions in one API request.

        Each entry has a stable `id`, structured `state`, `question`, and a
        bounded list of `{id, description, value}` options.
        """
        if not entries:
            return {}
        questions: dict[str, dict] = {}
        states: dict[str, Any] = {}
        option_maps: dict[str, dict[str, dict]] = {}
        for entry in entries:
            key = str(entry["id"])
            if key in questions:
                raise JevError(f"ID de decisão duplicado: {key}")
            options = entry.get("options") or []
            if not options or len(options) > 254:
                raise JevError(f"Quantidade de opções inválida para {key}")
            option_map = {str(opt["id"]): opt for opt in options}
            if len(option_map) != len(options) or "ask_user" in option_map:
                raise JevError(f"Opções duplicadas ou reservadas para {key}")
            option_map["ask_user"] = {
                "id": "ask_user",
                "description": "Nenhuma opção é segura; pedir esclarecimento ao usuário.",
                "value": None,
            }
            option_maps[key] = option_map
            states[key] = entry.get("state") or {}
            questions[key] = {
                "type": "choice",
                "instructions": entry["question"],
                "criteria": {opt_id: str(opt["description"]) for opt_id, opt in option_map.items()},
            }

        payload = {
            "model": "jev-latest",
            "state": {"decisions": states},
            "questions": questions,
        }
        request = urllib.request.Request(
            "https://api.typesafe.ai/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise JevError(f"Falha na consulta Jev: {exc}") from exc

        answers = data.get("answers") or {}
        result: dict[str, dict] = {}
        for key, option_map in option_maps.items():
            answer = answers.get(key) or {}
            choice = answer.get("choice")
            probabilities = answer.get("probabilities") or {}
            if choice not in option_map or not isinstance(probabilities, dict):
                raise JevError(f"Resposta Jev inválida para {key}")
            option = option_map[choice]
            result[key] = {
                "choice": choice,
                "confidence": answer.get("confidence"),
                "probabilities": {option_id: probabilities.get(option_id) for option_id in option_map},
                "candidate": option.get("value"),
                "model": data.get("model"),
            }
        return result


def is_confident_suggestion(
    suggestion: dict, *, min_probability: float = 0.90, min_margin: float = 0.20
) -> bool:
    """Require a candidate, high selected probability, confidence, and margin."""
    if not suggestion.get("candidate"):
        return False
    probabilities = suggestion.get("probabilities") or {}
    choice = suggestion.get("choice")
    try:
        selected = float(probabilities[choice])
        others = [float(value) for key, value in probabilities.items() if key != choice]
        confidence = float(suggestion.get("confidence") or 0)
    except (KeyError, TypeError, ValueError):
        return False
    runner_up = max(others, default=0.0)
    return (
        selected >= min_probability
        and confidence >= min_probability
        and selected - runner_up >= min_margin
    )


def compare_to_user_choice(suggestion: dict | None, user_index: int) -> dict | None:
    """Record an explicit user selection as an evaluation label."""
    if not suggestion or not suggestion.get("candidate"):
        return None
    try:
        jev_index = int(suggestion["candidate"]["index"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "jev_index": jev_index,
        "user_index": int(user_index),
        "match": jev_index == int(user_index),
        "jev_probability": (suggestion.get("probabilities") or {}).get(suggestion.get("choice")),
        "model": suggestion.get("model"),
    }
