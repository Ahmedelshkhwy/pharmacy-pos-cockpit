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
    business_type: str = "pharmacy"
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
    protect_volume_items: bool = True
    customer_loss_percent: float = 20.0
    supplier_min_order: float = 0.0
    road_factor: float = 1.3
    avg_speed_kmh: float = 30.0
    stop_minutes: int = 10
    shift_hours: float = 6.0
    cost_per_km: float = 0.0
    stale_data_days: int = 2
    # [{"kind": "supplier" | "category", "name": "...", "days": 3}]
    lead_time_overrides: list[dict[str, Any]] = field(default_factory=list)

    def service_class(self, abc_class: str = "", volume_class: str = "") -> str:
        """The class that sets availability: the profit class, or the quantity class when it is higher.

        A low-profit item that sells in large numbers (syringes, paracetamol) brings customers in,
        so with protect_volume_items it keeps the availability of its quantity class.
        """
        if self.protect_volume_items and volume_class and (not abc_class or "ABC".index(volume_class) < "ABC".index(abc_class)):
            return volume_class
        return abc_class

    def safety_z(self, abc_class: str = "", volume_class: str = "") -> float:
        """Safety stock factor: A items get the highest availability, C items the lowest."""
        level = {"A": self.service_level_a, "C": self.service_level_c}.get(self.service_class(abc_class, volume_class), self.service_level_b)
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
        if (data or {}).get("business_type") in BUSINESS_PROFILES:
            settings.business_type = data["business_type"]
        return settings

    def with_profile(self, business_type: str) -> "AnalysisSettings":
        """Suggested values for a business type; supplier/category lead times are kept."""
        profile = BUSINESS_PROFILES.get(business_type, BUSINESS_PROFILES["pharmacy"])
        updated = AnalysisSettings.from_dict({**AnalysisSettings().to_dict(), **profile["preset"]})
        updated.business_type = business_type if business_type in BUSINESS_PROFILES else "pharmacy"
        updated.lead_time_overrides = list(self.lead_time_overrides)
        return updated


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
    "transport": ("النقل بين الفروع", "Transport between branches"),
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
    SettingField("protect_volume_items", "supply", "احمِ الأصناف الكتير البيع", "Protect high-volume items",
                 "صنف بيتباع كميات كبيرة وربحه قليل (زي السرنجة) بيجيب زباين، فياخد نسبة التوفر بتاعة تصنيفه في الكمية لو أعلى من تصنيفه في الربح.",
                 "An item that sells in large numbers with little profit (like syringes) brings customers in, so it gets the availability of its quantity class when that is higher than its profit class.", kind="bool"),
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
    SettingField("customer_loss_percent", "profit", "خسارة الزبون لما الصنف يخلص", "Customer loss when an item runs out",
                 "لما الزبون ميلاقيش الصنف ممكن ميرجعش. الرقم ده نسبة من سعر البيع بتتحسب خسارة فوق الربح الضايع، في خطة الشراء بالميزانية. 0 يعني الربح الضايع بس.",
                 "A customer who does not find the item may not come back. This share of the sale price counts as a loss on top of the lost profit, in the budget purchase plan. 0 counts lost profit only.", 0, 500, 1, "%", "%"),
    SettingField("supplier_min_order", "supply", "أقل طلبية عند المورد", "Supplier minimum order",
                 "في خطة الشراء بالميزانية: طلبية المورد يا إما متتعملش، يا إما تكون بالقيمة دي على الأقل. 0 يعني مفيش حد أدنى. الأصناف اللي مالهاش مورد مش بتتأثر.",
                 "In the budget purchase plan, an order to a supplier is either skipped or at least this value. 0 means no minimum. Items without a supplier are not affected.", 0, 10_000_000, 50, "بالعملة", "in currency"),
    SettingField("road_factor", "transport", "معامل الطرق", "Road factor",
                 "الطريق الحقيقي أطول من الخط المستقيم على الخريطة. 1.3 مناسب جوه المدن. أي مسافة تكتبها بإيدك في صفحة الفروع بتغلب الحساب ده.",
                 "Real roads are longer than the straight line on the map; 1.3 suits cities. Any distance you enter on the branches page wins over this.", 1, 3, 0.05, "×", "×"),
    SettingField("avg_speed_kmh", "transport", "متوسط سرعة العربية", "Average vehicle speed",
                 "بيحوّل الكيلومترات لوقت، بالزحمة. 30 كم/ساعة مناسب جوه المدن.", "Turns kilometres into time, traffic included. 30 km/h suits cities.", 5, 120, 1, "كم/ساعة", "km/h"),
    SettingField("stop_minutes", "transport", "وقت الوقفة عند كل فرع", "Time at each stop",
                 "التحميل والتسليم والتوقيع عند كل فرع.", "Loading, unloading and signing at each branch.", 0, 120, 1, "دقيقة", "min"),
    SettingField("shift_hours", "transport", "مدة وردية السواق", "Driver shift",
                 "أقصى وقت للمشوار اليومي من أول ما العربية تطلع لحد ما ترجع.", "Longest daily trip, from leaving until returning.", 1, 16, 0.5, "ساعة", "hours"),
    SettingField("cost_per_km", "transport", "تكلفة الكيلو", "Cost per kilometre",
                 "بنزين وصيانة ونصيب السواق. بيه الأداة بتقارن قيمة التحويل بتكلفة توصيله. 0 يعني الوقت بس هو اللي بيحدّ.",
                 "Fuel, upkeep and the driver's share. It lets the tool weigh a transfer's value against the cost of moving it. 0 means only time limits the trip.", 0, 1000, 0.1, "بالعملة", "in currency"),
    SettingField("stale_data_days", "transport", "بيانات الفرع قديمة بعد", "Branch data is stale after",
                 "الفرع اللي آخر ملف ليه أقدم من كده مش بيدخل في خطة النقل، علشان رصيده ممكن يكون اتغير.",
                 "A branch whose last file is older than this is left out of the transport plan, since its stock may have changed.", 0, 60),
    SettingField("correct_stockouts", "forecast", "صحّح أيام النقص", "Correct for stockout days",
                 "لما الصنف كان ناقص، الأيام دي متتحسبش ضمن معدل البيع. بيشتغل مع ملفات الفواتير.", "Days the item was out of stock are excluded from its sales rate. Works with invoice files.", kind="bool"),
    SettingField("recency_half_life_days", "forecast", "وزن الأيام الأخيرة", "Recent-days weight (half-life)",
                 "البيع من قبل المدة دي ليه نص وزن البيع النهارده. 0 يعني كل الأيام بنفس الوزن. 30 كانت الأدق في الاختبار على بيانات حقيقية.",
                 "Sales this many days ago count half as much as today. 0 weighs all days equally. 30 tested best on real data.", 0, 365),
]


BUSINESS_PROFILES: dict[str, dict[str, Any]] = {
    "pharmacy": {
        "label": ("صيدلية", "Pharmacy"),
        "description": ("كابينة قرارات لمخزون الصيدليات، مخاطر المبيعات، وأداء الفروع.", "A decision cockpit for pharmacy stock, sales risk, and branch performance."),
        "note": ("صلاحيات مهمة، وتوريد أسبوعي تقريباً، وأصناف A لازم متخلصش.", "Expiry matters, roughly weekly supply, A items must not run out."),
        "preset": {},
    },
    "supermarket": {
        "label": ("سوبر ماركت", "Supermarket / grocery"),
        "description": ("كابينة قرارات لمخزون السوبر ماركت: الدوران، النواقص، والهالك.", "A decision cockpit for grocery stock: turnover, stockouts, and waste."),
        "note": ("توريد سريع، وصلاحيات قصيرة، وهوامش ضيقة.", "Fast supply, short shelf life, thin margins."),
        "preset": {"lead_time_days": 3, "target_cover_days": 14, "slow_moving_cover_days": 45, "overstock_cover_days": 90, "excess_cover_days": 45,
                   "fast_min_units": 50, "near_expiry_days": 30, "expiry_horizon_days": 60, "thin_margin_percent": 5},
    },
    "cosmetics": {
        "label": ("مستحضرات تجميل وعناية", "Cosmetics & personal care"),
        "description": ("كابينة قرارات لمخزون مستحضرات التجميل: الربح، والراكد، والصلاحية.", "A decision cockpit for cosmetics stock: margin, idle stock, and expiry."),
        "note": ("هوامش أعلى، وصلاحيات طويلة، وتوريد أبطأ.", "Higher margins, long shelf life, slower supply."),
        "preset": {"lead_time_days": 10, "target_cover_days": 45, "slow_moving_cover_days": 120, "overstock_cover_days": 240, "excess_cover_days": 120,
                   "fast_min_units": 10, "near_expiry_days": 120, "expiry_horizon_days": 270, "thin_margin_percent": 15},
    },
    "spare_parts": {
        "label": ("قطع غيار", "Spare parts"),
        "description": ("كابينة قرارات لمخزون قطع الغيار: التوفر، والراكد، والفلوس المتجمدة.", "A decision cockpit for spare parts: availability, idle stock, and tied-up cash."),
        "note": ("مفيش صلاحيات غالباً، وتوريد طويل، وحركة بطيئة طبيعية.", "Usually no expiry, long lead times, slow movement is normal."),
        "preset": {"lead_time_days": 21, "target_cover_days": 60, "slow_moving_cover_days": 180, "overstock_cover_days": 365, "excess_cover_days": 180,
                   "fast_min_units": 5, "thin_margin_percent": 10, "service_level_a": 97, "service_level_b": 93, "service_level_c": 85},
    },
    "general": {
        "label": ("تجارة تجزئة عامة", "General retail"),
        "description": ("كابينة قرارات للمخزون والمبيعات وأداء الفروع.", "A decision cockpit for stock, sales, and branch performance."),
        "note": ("قيم متوسطة تنفع كبداية لأي نشاط.", "Middle-of-the-road values, a good start for any business."),
        "preset": {"lead_time_days": 7, "target_cover_days": 30, "slow_moving_cover_days": 90, "overstock_cover_days": 180, "excess_cover_days": 90,
                   "fast_min_units": 20, "thin_margin_percent": 10},
    },
}


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


assert {spec.key for spec in SETTING_FIELDS} == {f.name for f in fields(AnalysisSettings)} - {"lead_time_overrides", "business_type"}
