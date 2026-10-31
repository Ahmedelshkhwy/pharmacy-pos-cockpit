"""Ask your data: built-in parsing, branch scoping, and the Claude tool loop (with a fake client)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import ask  # noqa: E402
from test_web_flow import client, submit_review, upload  # noqa: E402,F401

ROWS = [
    {"branch": "Maadi", "sku": "P1", "name": "Panadol Extra", "category": "Analgesics", "supplier": "GSK", "tags": "dead_stock", "abc_class": "",
     "frozen_capital_value": 900, "net_sales": 0, "gross_profit": 0, "stock": 40, "sold": 0, "cost_known": 1, "cost_price": 30, "price": 36},
    {"branch": "Maadi", "sku": "P2", "name": "Augmentin 1g", "category": "Antibiotics", "supplier": "GSK", "tags": "stockout_with_sales", "abc_class": "A",
     "lost_profit_value": 500, "net_sales": 9000, "gross_profit": 1500, "stock": 0, "sold": 80, "cost_known": 1, "stockout_probability_14d": 1.0,
     "recommended_reorder_qty": 40, "cost_price": 100, "price": 118},
    {"branch": "Zayed", "sku": "P3", "name": "Vichy Mineral", "category": "Cosmetics", "supplier": "Beauty Hub", "tags": "negative_margin", "abc_class": "A",
     "negative_margin_loss": 300, "net_sales": 20000, "gross_profit": 4000, "stock": 12, "sold": 25, "cost_known": 1, "cost_price": 600, "price": 590},
]


def parsed(question):
    return ask.parse_question(question, ROWS)


def test_parser_understands_common_questions():
    q = parsed("إيه أكتر 2 أصناف بتخسرني فلوس في Maadi؟")
    assert (q.metric, q.order, q.limit, q.branch) == ("loss", "desc", 2, "Maadi")
    assert parsed("أنهي فرع عنده أكبر فلوس متجمدة؟").group_by == "branch"
    assert parsed("أقل الفروع في الربح").group_by == "branch" and parsed("أقل الفروع في الربح").order == "asc"
    q = parsed("كام صنف راكد؟")
    assert q.count_only and q.tag == "dead" and q.metric == "frozen"
    q = parsed("كام صنف بيتباع بأقل من التكلفة")
    assert q.tag == "below_cost" and q.order == "desc"
    assert parsed("أصناف A اللي ممكن تخلص").abc_class == "A"
    assert parsed("Panadol بيبيع كام في اليوم").item == "Panadol Extra"
    assert parsed("Which supplier do we buy most from?").group_by == "supplier"
    assert parsed("١٠ أصناف أعلى مبيعات").limit == 10  # Arabic digits


def test_answers_rank_and_group():
    answer = ask.answer_builtin("أكتر صنف بيخسرني", ROWS, "ar")
    assert answer.rows[0]["name"] == "Augmentin 1g"
    grouped = ask.answer_builtin("compare branches by sales", ROWS, "en")
    assert [row["group"] for row in grouped.rows] == ["Zayed", "Maadi"]
    count = ask.answer_builtin("how many dead stock items", ROWS, "en")
    assert count.headline.startswith("1 items")


def fake_claude(tool_input):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            block = SimpleNamespace(type="tool_use", id="toolu_1", name="query_items", input=tool_input)
            return SimpleNamespace(stop_reason="tool_use", content=[block])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="الإجابة من Claude")])

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))), calls


def test_claude_uses_only_the_safe_query_tool():
    query = {"metric": "sales", "order": "desc", "limit": 5, "group_by": "", "branch": "", "category": "", "supplier": "",
             "abc_class": "", "tag": "", "count_only": False, "item": ""}
    fake, calls = fake_claude(query)
    maadi_only = [row for row in ROWS if row["branch"] == "Maadi"]
    answer = ask.ask("ايه اكتر حاجة بتتباع", maadi_only, "ar", fake)
    assert answer.source == "claude" and answer.headline == "الإجابة من Claude"
    assert {row["branch"] for row in answer.rows} == {"Maadi"}  # the tool only sees the rows it was given
    first = calls[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert [tool["name"] for tool in first["tools"]] == ["query_items"]
    assert "Zayed" not in first["system"]  # other branches are not even named
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "toolu_1" and "Augmentin" in tool_result["content"]


def test_claude_failure_falls_back_to_builtin():
    def broken(**_kwargs):
        raise ConnectionError("offline")

    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=broken)))
    answer = ask.ask("أكتر صنف بيخسرني", ROWS, "ar", fake)
    assert answer.source == "built-in" and answer.rows and "ConnectionError" in answer.note


def test_ask_page_respects_branch_permissions(client):
    import app as appmod

    submit_review(client, upload(client, [ROOT / "sample_data" / "D_chain_multi_branch.xlsx"]))
    client.post("/users", data={"action": "create", "username": "zayed_mgr", "password": "password123", "role": "manager", "branches": ["Zayed"]})
    manager = appmod.app.test_client()
    manager.post("/login", data={"username": "zayed_mgr", "password": "password123"})
    page = manager.get("/ask", query_string={"q": "أكتر 10 أصناف بتخسرني فلوس في Downtown"}).data.decode()
    assert "Downtown</td>" not in page and "Nasr City" not in page
    owner_page = client.get("/ask", query_string={"q": "compare branches by sales"}).data.decode()
    assert "Downtown" in owner_page and "Nasr City" in owner_page
