"""Analysis settings: every threshold the analysis uses, with defaults, limits and labels.

Settings are stored as JSON in the app database and passed to the pipeline on each import,
so changing them never needs a code change. Defaults reproduce the original behaviour.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from statistics import NormalDist
from typing import Any


@dataclass
class AnalysisSettings:
    lead_time_days: int = 7
    target_cover_days: int = 30
    service_level_a: float = 98.0
    service_level_b: float = 95.0
    service_level_c: float = 90.0
    slow_moving_cover_days: int = 90
    overstock_cover_days: int = 180
    excess_cover_days: int = 90
    fast_min_units: float = 20.0
    near_expiry_days: int = 90
    expiry_horizon_days: int = 180
    thin_margin_percent: float = 8.0
    correct_stockouts: bool = True
    recency_half_life_days: int = 30
    # [{"kind": "supplier" | "category", "name": "...", "days": 3}]
    lead_time_overrides: list[dict[str, Any]] = field(default_factory=list)

    def safety_z(self, abc_class: str = "") -> float:
        """Safety stock factor: A items get the highest availability, C items the lowest."""
        level = {"A": self.service_level_a, "C": self.service_level_c}.get(abc_class, self.service_level_b)
        return NormalDist().inv_cdf(level / 100)

    def lead_time_for(self, supplier: str, category: str) -> int:
        """Supplier override beats category override beats the default."""
        for kind, value in (("supplier", supplier), ("category", category)):
            for override in self.lead_time_overrides:
                if override.get("kind") == kind and value and str(override.get("name", "")).strip().casefold() == value.strip().casefold():
                    return int(override["days"])
        return self.lead_time_days

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AnalysisSettings":
        settings = cls()
        if data and "service_level_percent" in data and "service_level_b" not in data:
            data = {**data, "service_level_b": data["service_level_percent"]}  # saved before per-class levels
        for spec in SETTING_FIELDS:
            if data and spec.key in data:
                setattr(settings, spec.key, spec.coerce(data[spec.key]))
        settings.lead_time_overrides = clean_overrides((data or {}).get("lead_time_overrides", []))
        return settings


@dataclass(frozen=True)
class SettingField:
    key: str
    group: str
    label_ar: str
    label_en: str
    help_ar: str
    help_en: str
    minimum: float = 0
    maximum: float = 3650
    step: float = 1
    unit_ar: str = "يوم"
    unit_en: str = "days"
    kind: str = "number"  # number | bool

    def coerce(self, value: Any) -> Any:
        default = getattr(AnalysisSettings(), self.key)
        if self.kind == "bool":
            return str(value).lower() in {"1", "true", "on", "yes"} if not isinstance(value, bool) else value
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        number = min(max(number, self.minimum), self.maximum)
        return int(round(number)) if isinstance(default, int) else number


SETTING_GROUPS = {
    "supply": ("الطلب والتوريد", "Ordering & supply"),
    "movement": ("تصنيف الحركة", "Movement classes"),
    "expiry": ("الصلاحية", "Expiry"),
    "profit": ("الربحية", "Profitability"),
    "forecast": ("حساب معدل البيع", "Sales rate"),
}

SETTING_FIELDS = [
    SettingField("lead_time_days", "supply", "مدة التوريد", "Supplier lead time",
                 "من وقت ما تطلب لحد ما البضاعة توصل الرف. بتحدد امتى لازم تطلب.", "From ordering until stock is on the shelf. Decides when to reorder.", 1, 120),
    SettingField("target_cover_days", "supply", "الطلبية تكفي كام يوم", "Order covers",
                 "كمية الطلب المقترحة تكفي المدة دي بعد وصولها.", "Suggested order quantity covers this many days after arrival.", 1, 365),
    SettingField("service_level_a", "supply", "نسبة التوفر لأصناف A", "Availability for A items",
                 "أصناف A بتعمل 80% من الربح، فنقصها مكلف. نسبة أعلى معناها مخزون أمان أكبر.", "A items make 80% of profit, so running out is costly. Higher means more safety stock.", 50, 99.9, 0.5, "%", "%"),
    SettingField("service_level_b", "supply", "نسبة التوفر لأصناف B", "Availability for B items",
                 "الأصناف المتوسطة، والأصناف اللي مالهاش تصنيف.", "Mid items, and items without a class.", 50, 99.9, 0.5, "%", "%"),
    SettingField("service_level_c", "supply", "نسبة التوفر لأصناف C", "Availability for C items",
                 "أصناف C ربحها قليل. نسبة أقل بتقلل الفلوس المتجمدة فيها.", "C items earn little; a lower level frees cash tied up in them.", 50, 99.9, 0.5, "%", "%"),
    SettingField("slow_moving_cover_days", "movement", "بطيء لو الرصيد يغطي أكتر من", "Slow if stock covers more than",
                 "صنف بيتباع بس الرصيد بتاعه يكفي أكتر من المدة دي.", "Item sells but stock lasts longer than this.", 7, 3650),
    SettingField("overstock_cover_days", "movement", "زيادة مخزون لو يغطي أكتر من", "Overstock if stock covers more than",
                 "الرصيد كبير جداً مقارنة بالبيع، فوقّف الشراء.", "Stock far above demand: stop buying.", 30, 3650),
    SettingField("excess_cover_days", "movement", "الاحتياج الطبيعي للمخزون", "Normal stock need",
                 "أي رصيد فوق احتياج المدة دي بيتحسب فلوس متجمدة في صفحة المكسب والخسارة.", "Stock above this many days of demand counts as frozen cash.", 14, 3650),
    SettingField("fast_min_units", "movement", "سريع لو اتباع على الأقل", "Fast if sold at least",
                 "أقل كمية مبيعة في الفترة علشان الصنف يتصنف سريع.", "Minimum units sold in the period to count as fast moving.", 1, 100000, 1, "وحدة", "units"),
    SettingField("near_expiry_days", "expiry", "قرب انتهاء الصلاحية", "Near expiry within",
                 "الصنف يظهر في تنبيه الصلاحية لو فاضل له أقل من كده.", "Flag items expiring within this many days.", 7, 730),
    SettingField("expiry_horizon_days", "expiry", "مدى حساب خطر الصلاحية", "Expiry risk horizon",
                 "الكميات اللي هتنتهي خلال المدة دي قبل ما تتباع بتتحسب خسارة متوقعة.", "Units expiring within this window before they sell count as expected loss.", 14, 1095),
    SettingField("thin_margin_percent", "profit", "هامش ضعيف لو أقل من", "Thin margin below",
                 "الأصناف الأكثر مبيعاً بهامش أقل من كده بتظهر في الملاحظات.", "Best sellers under this margin are flagged.", 0, 100, 0.5, "%", "%"),
    SettingField("correct_stockouts", "forecast", "صحّح أيام النقص", "Correct for stockout days",
                 "لما الصنف كان ناقص، الأيام دي متتحسبش ضمن معدل البيع. بيشتغل مع ملفات الفواتير.", "Days the item was out of stock are excluded from its sales rate. Works with invoice files.", kind="bool"),
    SettingField("recency_half_life_days", "forecast", "وزن الأيام الأخيرة", "Recent-days weight (half-life)",
                 "البيع من قبل المدة دي ليه نص وزن البيع النهارده. 0 يعني كل الأيام بنفس الوزن. 30 كانت الأدق في الاختبار على بيانات حقيقية.",
                 "Sales this many days ago count half as much as today. 0 weighs all days equally. 30 tested best on real data.", 0, 365),
]


def clean_overrides(raw: Any) -> list[dict[str, Any]]:
    overrides = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        name = " ".join(str(entry.get("name") or "").split())
        try:
            days = int(float(entry.get("days")))
        except (TypeError, ValueError):
            continue
        if kind in {"supplier", "category"} and name and 1 <= days <= 365:
            overrides.append({"kind": kind, "name": name[:80], "days": days})
    return overrides


def dumps(settings: AnalysisSettings) -> str:
    return json.dumps(settings.to_dict(), ensure_ascii=False)


def loads(text: str | None) -> AnalysisSettings:
    try:
        return AnalysisSettings.from_dict(json.loads(text) if text else None)
    except json.JSONDecodeError:
        return AnalysisSettings()


assert {spec.key for spec in SETTING_FIELDS} == {f.name for f in fields(AnalysisSettings)} - {"lead_time_overrides"}
