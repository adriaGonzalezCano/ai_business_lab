"""Provider registry."""
from __future__ import annotations

from ..config import Config
from .anthropic import AnthropicProvider
from .base import Provider, ProviderError, ProviderRequest, ProviderResult, ProviderUnavailable
from .mock import MockProvider

__all__ = ["build_providers", "Provider", "ProviderError", "ProviderRequest", "ProviderResult", "ProviderUnavailable"]


def build_providers(config: Config) -> dict[str, Provider]:
    return {
        "mock": MockProvider(),
        "anthropic": AnthropicProvider(config.anthropic_api_key, config.anthropic_base_url),
    }
