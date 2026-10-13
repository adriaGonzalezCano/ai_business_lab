"""Integration capability registry.

Rule: an integration that is not configured reports itself *unavailable* with a setup guide. Nothing here
ever simulates a successful external action. Phase 3 adds real adapters behind the same `require()` gate.
"""
from __future__ import annotations

from typing import Any

from .config import Config


class IntegrationUnavailable(RuntimeError):
    def __init__(self, integration_id: str, guide: str):
        super().__init__(f"Integration '{integration_id}' is unavailable. {guide}")
        self.integration_id, self.guide = integration_id, guide


def _entry(id_: str, name: str, status: str, capabilities: list[str], auth: str, permissions: str, pricing: str,
           restrictions: str, failure: str, approval: bool, guide: str) -> dict[str, Any]:
    return {"id": id_, "name": name, "status": status, "capabilities": capabilities, "auth": auth,
            "permissions_required": permissions, "pricing_and_limits": pricing, "platform_restrictions": restrictions,
            "failure_handling": failure, "requires_human_approval": approval, "setup_guide": guide}


class Integrations:
    def __init__(self, config: Config):
        self.config = config

    def catalog(self) -> list[dict[str, Any]]:
        key = bool(self.config.anthropic_api_key)
        planned = "Not built yet (planned for Phase 3). It will not simulate success; it stays unavailable until implemented."
        return [
            _entry("web_research", "Robots-aware web fetcher", "available",
                   ["fetch public http(s) pages", "extract text", "respect robots.txt", "per-host rate limit"],
                   "none", "network egress to the sites you list as seeds", "free; subject to each site's terms",
                   "Respects robots.txt (fail closed), no logins, no circumvention, private addresses refused.",
                   "Returns a per-URL error; never invents page content.", False,
                   "Nothing to configure. Add seed URLs under Research."),
            _entry("model_mock", "Mock model provider", "available",
                   ["deterministic synthetic output for tests and demos"], "none", "none", "free",
                   "Output is synthetic and labelled as such everywhere.", "n/a", False, "Nothing to configure."),
            _entry("model_anthropic", "Anthropic Messages API", "available" if key else "unconfigured",
                   ["structured agent calls"], "API key in ANTHROPIC_API_KEY (server environment only)",
                   "an Anthropic account with API access",
                   "Pay per token. Enter model prices in Settings > model_pricing; calls are refused without a price. "
                   "No hard account-level cap exists via the API: set a limit in the Anthropic console too.",
                   "Subject to Anthropic usage policy and rate limits.",
                   "Failed calls release their budget reservation; bounded retries.", False,
                   "Set ANTHROPIC_API_KEY in .env, restart, set model_pricing, then choose provider 'anthropic' in Settings."),
            _entry("search_api", "Search API (e.g. a web search provider)", "unconfigured",
                   ["discover candidate source URLs"], "provider API key", "provider account", "provider-dependent",
                   "Provider terms apply.", "n/a", False,
                   "Not built yet. Until then, provide seed URLs manually under Settings > research_seeds."),
            _entry("storefront", "Digital-product storefront / marketplace", "not_built",
                   ["list products", "read sales"], "provider OAuth/API key", "seller account", "provider fees",
                   "Many marketplaces restrict automated listing or AI-generated assets; check terms first.",
                   "n/a", True, planned),
            _entry("payments", "Payment processor", "not_built", ["read payments", "refunds"], "provider API key",
                   "merchant account", "per-transaction fees", "KYC and account requirements.", "n/a", True, planned),
            _entry("image_generation", "Image generation service", "not_built", ["generate assets"], "API key",
                   "account", "per image", "Licence and platform rules on AI-generated art.", "n/a", False, planned),
            _entry("analytics", "Web analytics", "not_built", ["visitors", "conversions"], "API key", "property access",
                   "free tiers vary", "Privacy / consent rules (GDPR).", "n/a", False, planned),
            _entry("email_support", "Email and customer support", "not_built", ["send/receive email"], "SMTP/API",
                   "mailbox", "provider-dependent", "Anti-spam and privacy law.", "n/a", True, planned),
            _entry("cloud_storage", "Cloud storage", "not_built", ["store assets"], "API key", "bucket", "per GB",
                   "n/a", "n/a", False, planned),
            _entry("accounting_export", "Accounting export", "not_built", ["export ledger"], "none", "none", "free",
                   "Not tax advice.", "n/a", False, planned),
            _entry("workflow_automation", "Workflow automation tools", "not_built", ["trigger external workflows"],
                   "webhook/API key", "account", "provider-dependent", "n/a", "n/a", True, planned),
        ]

    def get(self, integration_id: str) -> dict[str, Any]:
        for e in self.catalog():
            if e["id"] == integration_id:
                return e
        raise KeyError(integration_id)

    def require(self, integration_id: str) -> dict[str, Any]:
        e = self.get(integration_id)
        if e["status"] != "available":
            raise IntegrationUnavailable(integration_id, e["setup_guide"])
        return e
