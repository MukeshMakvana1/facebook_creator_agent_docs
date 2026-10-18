"""License policy: default-deny evaluation of asset rights (FR-032/033,
loop-engineering §6).

Outcomes:
* ``APPROVED`` — provider on the allowlist AND license on the allowlist AND
  attribution requirements satisfied. Only APPROVED assets are eligible for
  automatic download/use.
* ``REVIEW_REQUIRED`` — unknown/ambiguous rights (missing license, unknown
  label, missing required attribution). Excluded from automatic selection.
* ``REJECTED`` — provider not allowed, or a license that contradicts the
  intended use (non-commercial, no-derivatives, all-rights-reserved, unknown
  placeholder labels).

This is NOT legal verification; it is evidence-based gating (see PRD §7.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import AssetCandidate

#: Licenses accepted for automatic use (social-media/commercial-permitting,
#: well-known open licenses + explicit user-confirmed intake).
DEFAULT_ALLOWED_LICENSES: frozenset[str] = frozenset(
    {
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-3.0",
        "PUBLIC-DOMAIN",
        "USER-SUPPLIED-CONFIRMED",
        "PEXELS-LICENSE",  # documented commercial-use stock license (verify per asset)
    }
)

#: Licenses that require attribution text to be present.
ATTRIBUTION_REQUIRED: frozenset[str] = frozenset({"CC-BY-4.0", "CC-BY-3.0"})

#: Markers that mean "cannot be used automatically". Short tokens (nc/nd)
#: match only as whole dash/space-delimited words to avoid false positives.
_REJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[^a-z0-9])nc(?:[^a-z0-9]|$)"),
    re.compile(r"(?:^|[^a-z0-9])nd(?:[^a-z0-9]|$)"),
    re.compile(r"non[- ]?commercial"),
    re.compile(r"no[- ]?deriv"),
    re.compile(r"all rights reserved"),
    re.compile(r"editorial"),
    re.compile(r"unknown"),
    re.compile(r"unlicensed"),
    re.compile(r"proprietary"),
)


@dataclass
class PolicyResult:
    rights_status: str
    reasons: list[str] = field(default_factory=list)


class LicensePolicy:
    def __init__(
        self,
        *,
        allowed_providers: set[str],
        allowed_licenses: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.allowed_providers = {p.strip().lower() for p in allowed_providers}
        self.allowed_licenses = {
            lic.strip().upper() for lic in (allowed_licenses or DEFAULT_ALLOWED_LICENSES)
        }

    def evaluate(self, candidate: AssetCandidate, *, intended_use: str = "social_media") -> PolicyResult:
        provider = (candidate.provider or "").strip().lower()
        if provider not in self.allowed_providers:
            return PolicyResult(
                "REJECTED", [f"provider {candidate.provider!r} is not on the media provider allowlist"]
            )

        license_name = (candidate.license_name or "").strip()
        if not license_name:
            return PolicyResult(
                "REVIEW_REQUIRED", ["no license metadata supplied; rights are unknown"]
            )

        lowered = license_name.lower()
        # Exact match against the allowlist wins before marker heuristics so that
        # e.g. "CC0-1.0" is never misread by a substring rule.
        if license_name.upper() in self.allowed_licenses:
            if license_name.upper() in ATTRIBUTION_REQUIRED and not (candidate.attribution or "").strip():
                return PolicyResult(
                    "REVIEW_REQUIRED",
                    [f"license {license_name} requires attribution but none is supplied"],
                )
            return PolicyResult(
                "APPROVED",
                [
                    f"provider {provider!r} and license {license_name!r} are allowlisted",
                    f"intended use recorded: {intended_use}",
                ],
            )

        for pattern in _REJECT_PATTERNS:
            if pattern.search(lowered):
                return PolicyResult(
                    "REJECTED",
                    [
                        "license "
                        f"{license_name!r} contradicts the intended use ({intended_use}); "
                        "it is excluded from automatic selection"
                    ],
                )

        return PolicyResult(
            "REVIEW_REQUIRED",
            [f"license {license_name!r} is not on the allowlist; manual review required"],
        )
