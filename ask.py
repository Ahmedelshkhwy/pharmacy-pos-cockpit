"""Ask your data in plain Arabic or English.

Two layers share one safe query function (`run_query`), which only reads the item rows the
current user may see:
- the built-in parser turns common questions into a query, free and offline;
- with an Anthropic API key, Claude turns any phrasing into the same query through a tool
  call and words the answer. Claude never sees raw SQL or the whole database, only the
  results of the queries it asks for.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from smart_import import ARABIC_DIGITS, normalize_text

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_MAX_TOOL_ROUNDS = 4


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def item_loss(row: dict[str, Any]) -> float:
    lost = num(row.get("lost_profit_value")) if num(row.get("cost_known")) else num(row.get("lost_sales_value"))
    return num(row.get("negative_margin_loss")) + num(row.get("expiry_risk_value")) + lost


# metric key: (value function, arabic label, english label, words that ask for it, is money)
METRICS: dict[str, tuple[Callable[[dict[str, Any]], float], str, str, list[str], bool]] = {
    "loss": (item_loss, "الخسارة", "Loss", ["خسار", "خسر", "بيخسر", "بتخسر", "نزيف", "loss", "losing", "lose"], True),
    "profit": (lambda r: num(r.get("gross_profit")), "مجمل الربح", "Gross profit", ["ربح", "مكسب", "كسب", "بيكسب", "profit", "earn"], True),
    "margin": (lambda r: num(r.get("gross_margin_percent")), "الهامش %", "Margin %", ["هامش", "margin"], False),
    "sales": (lambda r: num(r.get("net_sales")), "المبيعات", "Sales", ["مبيعات", "ايراد", "بيعت", "sales", "revenue", "selling", "sold"], True),
    "rate": (lambda r: num(r.get("avg_daily_sales")), "بيع/يوم", "Units/day", ["في اليوم", "يوميا", "يومي", "per day", "daily"], False),
    "qty": (lambda r: num(r.get("sold")), "الكمية المباعة", "Units sold", ["كميه", "وحده", "وحدات", "units", "quantity"], False),
    "frozen": (lambda r: num(r.get("frozen_capital_value")), "فلوس متجمدة", "Frozen cash", ["متجمد", "مجمد", "نايم", "حابس", "frozen", "idle", "tied up"], True),
    "stock_value": (lambda r: num(r.get("cost_value")) or num(r.get("stock_value")), "قيمة المخزون", "Stock value", ["قيمه المخزون", "قيمه الرصيد", "stock value", "inventory value"], True),
    "stock": (lambda r: num(r.get("stock")), "الرصيد", "Stock", ["رصيد", "مخزون", "موجود", "stock", "on hand", "inventory"], False),
    "expiry": (lambda r: num(r.get("expiry_risk_value")), "قيمة خطر الصلاحية", "Expiry risk value", ["صلاحي", "هينتهي", "منتهي", "expir"], True),
    "stockout": (lambda r: num(r.get("stockout_probability_14d")) * 100, "احتمال النقص 14 يوم %", "Stockout risk 14d %", ["ناقص", "نواقص", "نقص", "نفاد", "هيخلص", "خلص", "stockout", "out of stock", "run out", "running out"], False),
    "purchase": (lambda r: num(r.get("recommended_reorder_qty")) * (num(r.get("cost_price")) or num(r.get("price"))), "مشتريات مقترحة", "Suggested purchases",
                 ["نشتري", "اشتري", "شتري", "مشتريات", "شراء", "اطلب", "طلبيه", "purchase", "buy", "reorder", "order"], True),
    "risk": (lambda r: num(r.get("risk_score")), "درجة الخطورة", "Risk score", ["خطوره", "خطر", "risk"], False),
}
METRIC_ORDER = list(METRICS)  # earlier entries win when several metrics are mentioned

TAGS = {
    "dead": ("dead_stock", ["راكد", "ركد", "مبيتباعش", "dead"]),
    "stockout": ("stockout_with_sales", ["ناقص", "نواقص", "خلصان", "out of stock", "stockout"]),
    "expired": ("expired", ["منتهي الصلاحيه", "انتهت صلاحيت", "expired"]),
    "expiry_risk": ("expiry_risk", ["صلاحيته قربت", "قرب انتهاء", "هينتهي", "expiring", "near expiry"]),
    "slow": ("slow_moving", ["بطيء", "بطئ", "slow"]),
    "fast": ("fast_moving", ["سريع", "fast"]),
    "overstock": ("overstock_risk", ["زياده", "اوفر", "overstock", "excess"]),
    "below_cost": ("negative_margin", ["اقل من التكلفه", "تحت التكلفه", "بخساره", "below cost"]),
}

LOW_WORDS = ["اقل", "اضعف", "اصغر", "اوطي", "اسوا", "least", "lowest", "smallest", "worst", "fewest", "bottom"]
COUNT_WORDS = ["كام صنف", "عدد الاصناف", "كام منتج", "how many"]
GROUP_HINTS = ["انهي", "اي", "مين", "كل", "حسب", "لكل", "مقارنه", "قارن", "which", "per", "by", "each", "compare"]
GROUP_PLURALS = {
    "branch": ["الفروع", "branches", "stores"],
    "category": ["المجموعات", "الفئات", "الاقسام", "categories"],
    "supplier": ["الموردين", "الشركات", "suppliers", "vendors"],
}
GROUP_WORDS = {
    "branch": ["فرع", "الفروع", "فروع", "branch", "store"],
    "category": ["مجموعه", "مجموعات", "فئه", "قسم", "category", "categories", "group"],
    "supplier": ["مورد", "موردين", "شركه", "شركات", "supplier", "vendor", "company"],
}


@dataclass
class Query:
    metric: str = "loss"
    order: str = "desc"              # desc | asc
    limit: int = DEFAULT_LIMIT
    group_by: str = ""               # "" | branch | category | supplier
    branch: str = ""
    category: str = ""
    supplier: str = ""
    abc_class: str = ""              # "" | A | B | C
    tag: str = ""                    # key of TAGS
    count_only: bool = False
    item: str = ""

    def clean(self, branches: list[str]) -> "Query":
        self.metric = self.metric if self.metric in METRICS else "loss"
        self.order = "asc" if self.order == "asc" else "desc"
        self.limit = max(1, min(int(self.limit or DEFAULT_LIMIT), MAX_LIMIT))
        self.group_by = self.group_by if self.group_by in GROUP_WORDS else ""
        self.abc_class = self.abc_class.upper() if str(self.abc_class).upper() in {"A", "B", "C"} else ""
        self.tag = self.tag if self.tag in TAGS else ""
        if self.branch and self.branch not in branches:
            match = next((b for b in branches if normalize_text(b) == normalize_text(self.branch)), "")
            self.branch = match or self.branch  # an unknown branch simply returns no rows
        return self


@dataclass
class Answer:
    headline: str
    columns: list[tuple[str, str]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    understood: str = ""
    source: str = "built-in"
    query: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def _has(text: str, words: list[str]) -> bool:
    return any(normalize_text(word) in text for word in words)


def _match_value(text: str, values: list[str]) -> str:
    """Longest known value (branch, category, supplier) mentioned in the question."""
    found = [value for value in values if value and len(normalize_text(value)) >= 3 and normalize_text(value) in text]
    return max(found, key=len) if found else ""


def parse_question(question: str, rows: list[dict[str, Any]]) -> Query:
    text = " " + normalize_text(question.translate(ARABIC_DIGITS)) + " "
    branches = sorted({str(row["branch"]) for row in rows})
    categories = sorted({str(row.get("category") or "") for row in rows} - {"", "UNCATEGORIZED"})
    suppliers = sorted({str(row.get("supplier") or "") for row in rows} - {""})
    query = Query()

    query.branch = _match_value(text, branches)
    query.category = _match_value(text, categories)
    query.supplier = _match_value(text, suppliers)
    names = sorted({str(row.get("name") or "") for row in rows}, key=len, reverse=True)
    query.item = next((name for name in names if len(name) >= 5 and normalize_text(name) in text), "")
    if not query.item:  # first word of a product name ("panadol", "augmentin") is enough
        words = {word for word in text.split() if len(word) >= 5}
        query.item = next((name for name in names if normalize_text(name).split()[:1] and normalize_text(name).split()[0] in words), "")

    for key in METRIC_ORDER:
        if _has(text, METRICS[key][3]):
            query.metric = key
            break
    for key, (_tag, words) in TAGS.items():
        if _has(text, words):
            query.tag = key
            break
    if query.tag == "dead" and query.metric == "loss":
        query.metric = "frozen"
    if query.tag == "stockout" and query.metric in {"loss", "stock"}:
        query.metric = "stockout"
    if query.tag in {"expired", "expiry_risk"}:
        query.metric = "expiry"

    abc = re.search(r"\b(?:class|اصناف|صنف|فئه|تصنيف)\s*([abc])\b", text)
    if abc:
        query.abc_class = abc.group(1).upper()
    numbers = [int(n) for n in re.findall(r"\b(\d{1,3})\b", text) if 0 < int(n) <= MAX_LIMIT]
    if numbers:
        query.limit = numbers[0]
    order_text = text
    if query.tag:  # "بأقل من التكلفة" names a filter, not a sort direction
        for word in TAGS[query.tag][1]:
            order_text = order_text.replace(normalize_text(word), " ")
    query.order = "asc" if _has(order_text, LOW_WORDS) else "desc"
    query.count_only = _has(text, COUNT_WORDS)
    for group, words in GROUP_WORDS.items():
        # "أنهي فرع" / "per branch" or a plural ("الفروع", "suppliers") asks to compare groups.
        asks = (_has(text, GROUP_HINTS) and _has(text, words)) or _has(text, GROUP_PLURALS[group])
        if asks and not getattr(query, group):
            query.group_by = group
            break
    if query.item and query.group_by == "":
        query.limit = MAX_LIMIT
    return query.clean(branches)


def run_query(rows: list[dict[str, Any]], query: Query, lang: str = "ar") -> Answer:
    ar = lang == "ar"
    value_of, label_ar, label_en, _words, money = METRICS[query.metric]
    label = label_ar if ar else label_en
    selected = [
        row for row in rows
        if (not query.branch or row["branch"] == query.branch)
        and (not query.category or row.get("category") == query.category)
        and (not query.supplier or row.get("supplier") == query.supplier)
        and (not query.abc_class or row.get("abc_class") == query.abc_class)
        and (not query.tag or TAGS[query.tag][0] in str(row.get("tags")))
        and (not query.item or normalize_text(row.get("name") or "") == normalize_text(query.item))
    ]
    scope = [part for part in (query.branch, query.category, query.supplier, f"ABC {query.abc_class}" if query.abc_class else "") if part]
    if query.tag:
        scope.append(TAGS[query.tag][1][0] if ar else TAGS[query.tag][1][-1])
    understood = " · ".join([label, ("الأقل" if ar else "lowest") if query.order == "asc" else ("الأعلى" if ar else "highest")] + scope)
    fmt = (lambda v: f"{v:,.0f}") if money else (lambda v: f"{v:,.1f}")

    if not selected:
        return Answer(headline="مفيش أصناف تطابق السؤال ده." if ar else "No items match this question.", understood=understood, query=asdict(query))

    if query.count_only:
        total = sum(value_of(row) for row in selected)
        headline = (f"{len(selected):,} صنف" if ar else f"{len(selected):,} items") + (f"، إجمالي {label}: {fmt(total)}" if ar else f", total {label.lower()}: {fmt(total)}")
        return Answer(headline=headline, understood=understood, query=asdict(query))

    if query.group_by:
        groups: dict[str, dict[str, float]] = {}
        for row in selected:
            key = str(row.get(query.group_by) or ("—"))
            bucket = groups.setdefault(key, {"value": 0.0, "items": 0})
            bucket["value"] += value_of(row)
            bucket["items"] += 1
        if query.metric in {"margin", "stockout", "risk"}:  # averages, not sums
            for bucket in groups.values():
                bucket["value"] /= max(bucket["items"], 1)
        ordered = sorted(groups.items(), key=lambda kv: kv[1]["value"], reverse=query.order == "desc")[: query.limit]
        group_label = {"branch": ("الفرع", "Branch"), "category": ("المجموعة", "Category"), "supplier": ("المورد", "Supplier")}[query.group_by]
        top_name, top = ordered[0]
        headline = (f"{top_name} هو {'الأقل' if query.order == 'asc' else 'الأعلى'} في {label}: {fmt(top['value'])}" if ar
                    else f"{top_name} has the {'lowest' if query.order == 'asc' else 'highest'} {label.lower()}: {fmt(top['value'])}")
        return Answer(
            headline=headline,
            columns=[("group", group_label[0 if ar else 1]), ("value", label), ("items", "أصناف" if ar else "Items")],
            rows=[{"group": name, "value": fmt(values["value"]), "items": int(values["items"])} for name, values in ordered],
            understood=understood,
            query=asdict(query),
        )

    ordered = sorted(selected, key=value_of, reverse=query.order == "desc")
    if query.order == "desc" and query.metric not in {"stock", "margin"}:
        ordered = [row for row in ordered if value_of(row) > 0] or ordered[:1]
    ordered = ordered[: query.limit]
    top = ordered[0]
    if query.item:
        headline = (f"{top['name']}: {label} {fmt(value_of(top))} في {top['branch']}" if ar else f"{top['name']}: {label.lower()} {fmt(value_of(top))} at {top['branch']}")
    else:
        headline = (f"{'الأقل' if query.order == 'asc' else 'الأعلى'} في {label}: {top['name']} ({top['branch']}) بـ {fmt(value_of(top))}" if ar
                    else f"{'Lowest' if query.order == 'asc' else 'Highest'} {label.lower()}: {top['name']} ({top['branch']}) at {fmt(value_of(top))}")
    total = sum(value_of(row) for row in ordered)
    note = (f"إجمالي أول {len(ordered)}: {fmt(total)}" if ar else f"Total of these {len(ordered)}: {fmt(total)}") if money and len(ordered) > 1 else ""
    return Answer(
        headline=headline,
        columns=[("name", "الصنف" if ar else "Item"), ("branch", "الفرع" if ar else "Branch"), ("value", label),
                 ("stock", "الرصيد" if ar else "Stock"), ("decision", "القرار" if ar else "Decision")],
        rows=[{"name": row["name"], "sku": row.get("sku"), "branch": row["branch"], "value": fmt(value_of(row)),
               "stock": f"{num(row.get('stock')):,.0f}", "decision": row.get("decision_label") or ""} for row in ordered],
        understood=understood,
        query=asdict(query),
        note=note,
    )


def answer_builtin(question: str, rows: list[dict[str, Any]], lang: str = "ar") -> Answer:
    return run_query(rows, parse_question(question, rows), lang)


# ---------------------------------------------------------------------------
# Claude layer (optional)
# ---------------------------------------------------------------------------

QUERY_TOOL = {
    "name": "query_items",
    "description": (
        "Query the pharmacy/retail item analysis the user is allowed to see. Returns a short headline and up to "
        "`limit` rows (or grouped totals). Use it for every number you state; call it several times to compare."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "enum": list(METRICS), "description": "What to rank or total. loss = below-cost sales + expiry risk + profit lost to stockouts."},
            "order": {"type": "string", "enum": ["desc", "asc"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            "group_by": {"type": "string", "enum": ["", "branch", "category", "supplier"]},
            "branch": {"type": "string", "description": "Exact branch name, or empty for all allowed branches."},
            "category": {"type": "string"},
            "supplier": {"type": "string"},
            "abc_class": {"type": "string", "enum": ["", "A", "B", "C"]},
            "tag": {"type": "string", "enum": [""] + list(TAGS)},
            "count_only": {"type": "boolean"},
            "item": {"type": "string", "description": "Exact item name to look up, or empty."},
        },
        "required": ["metric", "order", "limit", "group_by", "branch", "category", "supplier", "abc_class", "tag", "count_only", "item"],
        "additionalProperties": False,
    },
    "strict": True,
}

SYSTEM_PROMPT = """You answer questions about a business's inventory and sales analysis.
Answer in the language and dialect of the question (Egyptian Arabic if the question is in Arabic).
Use the query_items tool for every figure; never estimate or invent numbers. If the data cannot answer
the question, say what is missing in one sentence. Keep answers short: the direct answer first, then at
most three bullet points with the reason or the action to take. Money values have no currency symbol.

Known values for filters:
{context}"""


def data_context(rows: list[dict[str, Any]]) -> str:
    def top_values(key: str, limit: int = 40) -> str:
        values = sorted({str(row.get(key) or "") for row in rows} - {"", "UNCATEGORIZED"})
        return ", ".join(values[:limit]) or "(none)"
    return f"branches: {top_values('branch')}\ncategories: {top_values('category')}\nsuppliers: {top_values('supplier')}\nitems: {len(rows)}"


def answer_with_claude(question: str, rows: list[dict[str, Any]], lang: str, client: Any) -> Answer:
    """Tool-use loop: Claude plans queries, we run them on the user's rows, Claude words the answer."""
    branches = sorted({str(row["branch"]) for row in rows})
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    last: Answer | None = None
    for _ in range(CLAUDE_MAX_TOOL_ROUNDS + 1):
        response = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT.format(context=data_context(rows)),
            tools=[QUERY_TOOL],
            messages=messages,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("refusal")
        tool_calls = [block for block in response.content if block.type == "tool_use"]
        if not tool_calls:
            text = "\n".join(block.text for block in response.content if block.type == "text").strip()
            answer = last or Answer(headline="")
            return Answer(headline=text or answer.headline, columns=answer.columns, rows=answer.rows, understood=answer.understood,
                          source="claude", query=answer.query, note=answer.note)
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in tool_calls:
            try:
                query = Query(**{key: value for key, value in dict(call.input).items() if key in Query.__dataclass_fields__}).clean(branches)
                last = run_query(rows, query, lang)
                payload = {"headline": last.headline, "note": last.note, "rows": last.rows[:query.limit]}
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(payload, ensure_ascii=False)})
            except (TypeError, ValueError) as exc:
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": f"Invalid query: {exc}", "is_error": True})
        messages.append({"role": "user", "content": results})
    raise RuntimeError("too_many_rounds")


def ask(question: str, rows: list[dict[str, Any]], lang: str = "ar", client: Any | None = None) -> Answer:
    """Claude when a client is given (falls back to the built-in parser on any failure)."""
    question = (question or "").strip()[:500]
    if not question:
        return Answer(headline="")
    if not rows:
        return Answer(headline="مفيش بيانات لسه. ارفع ملفاتك الأول." if lang == "ar" else "No data yet. Upload your files first.")
    if client is not None:
        try:
            return answer_with_claude(question, rows, lang, client)
        except Exception as exc:  # network, auth, refusal, rate limit: the built-in answer still works
            answer = answer_builtin(question, rows, lang)
            answer.note = ("مساعد Claude مش متاح دلوقتي، فدي إجابة المحرك الداخلي." if lang == "ar" else "Claude is unavailable right now; this is the built-in answer.") + f" ({type(exc).__name__})"
            return answer
    return answer_builtin(question, rows, lang)


EXAMPLES_AR = [
    "إيه أكتر 5 أصناف بتخسرني فلوس؟",
    "أنهي فرع عنده أكبر فلوس متجمدة؟",
    "كام صنف راكد في فرع {branch}؟",
    "أكتر أصناف بتكسب في {branch}",
    "أنهي مورد بنشتري منه أكتر؟",
    "الأصناف اللي صلاحيتها قربت",
    "أقل الأصناف في الهامش",
    "النواقص في {branch}",
]
EXAMPLES_EN = [
    "Top 5 items losing me money",
    "Which branch has the most frozen cash?",
    "How many dead stock items in {branch}?",
    "Most profitable items in {branch}",
    "Which supplier do we buy most from?",
    "Items near expiry",
    "Lowest margin items",
    "Stockouts in {branch}",
]
