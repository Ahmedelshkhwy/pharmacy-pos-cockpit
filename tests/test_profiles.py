"""Business profiles: suggested values per business type, kept overrides, and wording."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from settings import BUSINESS_PROFILES, AnalysisSettings  # noqa: E402
from smart_import import header_field_scores  # noqa: E402
from test_web_flow import client  # noqa: E402,F401


def test_profiles_apply_presets_and_keep_lead_time_overrides():
    current = AnalysisSettings(lead_time_overrides=[{"kind": "supplier", "name": "Bosch", "days": 30}])
    parts = current.with_profile("spare_parts")
    assert parts.business_type == "spare_parts" and parts.lead_time_days == 21 and parts.service_level_c == 85
    assert parts.lead_time_overrides == current.lead_time_overrides
    assert AnalysisSettings().with_profile("pharmacy").to_dict() == AnalysisSettings().to_dict()  # pharmacy = the defaults
    assert AnalysisSettings().with_profile("unknown").business_type == "pharmacy"
    for key, profile in BUSINESS_PROFILES.items():
        AnalysisSettings.from_dict(profile["preset"])  # every preset is valid


def test_spare_parts_headers_are_understood():
    assert max(header_field_scores("Part Number"), key=header_field_scores("Part Number").get) == "ref"
    assert max(header_field_scores("رقم القطعة"), key=header_field_scores("رقم القطعة").get) == "ref"


def test_choosing_a_business_type_changes_settings_and_wording(client):
    import app as appmod

    client.post("/language/ar")
    assert "الصيدليات" in client.get("/settings").data.decode()
    client.post("/settings", data={"action": "apply_profile", "business_type": "supermarket"})
    settings = appmod.load_analysis_settings()
    assert settings.business_type == "supermarket" and settings.lead_time_days == 3 and settings.near_expiry_days == 30
    page = client.get("/").data.decode()
    assert "السوبر ماركت" in page and "الصيدليات" not in page
    # Saving the detailed form keeps the business type.
    form = {key: str(value) for key, value in settings.to_dict().items() if key not in {"lead_time_overrides", "business_type"}}
    form.update({"business_type": "supermarket", "lead_time_days": "4", "correct_stockouts": "1"})
    client.post("/settings", data=form)
    assert appmod.load_analysis_settings().business_type == "supermarket" and appmod.load_analysis_settings().lead_time_days == 4
