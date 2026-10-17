"""Additional HW1 tool edge cases, using temporary worlds and no model calls."""

from __future__ import annotations

import pytest

from agent import db, tools
from agent.auth import AuthContext

SHOPPER = AuthContext(user_id=1, role="shopper")
MERCHANT = AuthContext(user_id=9001, role="merchant", store_id=1)
SUPPORT = AuthContext(user_id=9501, role="support")


@pytest.mark.parametrize("ctx", [SHOPPER, MERCHANT, SUPPORT])
@pytest.mark.parametrize("store", ["Juniper Home Goods", "JUNIPER-HOME-GOODS"])
def test_hw1_public_store_lookup(world, ctx, store):
    with db.connection() as conn:
        expected = db.get_store_by_name(conn, store)
    assert tools.get_store_info(ctx, store) == {
        "ok": True,
        "store": {
            "store_id": expected.id,
            "name": expected.name,
            "slug": expected.slug,
            "category": expected.category,
            "return_window_days_override": expected.return_window_days_override,
            "restocking_fee_opt_in": expected.restocking_fee_opt_in,
        },
    }


def test_hw1_public_store_lookup_errors_and_default_policy(world):
    assert tools.get_store_info(SHOPPER, " \t ")["error"] == "invalid_argument"
    missing = tools.get_store_info(SHOPPER, "No Such Store")
    assert missing["error"] == "not_found"
    assert "No Such Store" in missing["reason"]
    result = tools.get_store_info(SHOPPER, "Blue Heron Ceramics")
    assert result["store"]["return_window_days_override"] is None


@pytest.mark.parametrize("ctx", [SHOPPER, MERCHANT, SUPPORT])
def test_hw1_public_store_lookup_is_registered(ctx):
    from agent.agent import TOOLS_BY_ROLE

    assert "get_store_info" in [tool.name for tool in TOOLS_BY_ROLE[ctx.role]]


@pytest.mark.parametrize("ctx", [SHOPPER, MERCHANT, SUPPORT])
def test_hw1_policy_is_public_and_exact(world, ctx):
    result = tools.get_policy(ctx, "cw-returns")
    assert set(result) == {"ok", "policy_id", "title", "audience", "body"}
    assert result["ok"] is True
    missing = tools.get_policy(ctx, "CW-RETURNS")
    assert missing["error"] == "not_found"
    assert "CW-RETURNS" in missing["reason"]


@pytest.mark.parametrize("limit,expected_count", [(0, 1), (2, 2), (100, 25)])
def test_hw1_product_filters_sort_and_limit(world_copy, limit, expected_count):
    with db.connection() as conn:
        products = db.list_products(conn, store_id=1)
        conn.execute(
            "UPDATE products SET title = 'Catalog Fixture', "
            "description = 'DescriptionKeyword', price_cents = 1000 WHERE store_id = 1"
        )
        conn.execute(
            "UPDATE products SET price_cents = 1001 WHERE id = ?", (products[0].id,)
        )
        conn.commit()
    result = tools.search_products(
        SHOPPER,
        " cAtAlOg   DESCRIPTIONkeyword ",
        store="BLUE-HERON-CERAMICS",
        max_price_usd=10,
        limit=limit,
    )
    assert result["count"] == expected_count
    assert result["products"] == [
        {
            "product_id": product.id,
            "store_id": 1,
            "title": "Catalog Fixture",
            "price_usd": 10.0,
        }
        for product in products[1 : expected_count + 1]
    ]
    empty = tools.search_products(SHOPPER, "Catalog nonexistentkeyword")
    assert empty == {"ok": True, "products": [], "count": 0}


@pytest.mark.parametrize("ceiling", [0, -1, float("nan")])
def test_hw1_product_search_rejects_nonpositive_ceiling(world, ceiling):
    result = tools.search_products(SHOPPER, "mug", max_price_usd=ceiling)
    assert result["error"] == "invalid_argument"
    assert result["reason"]


def test_hw1_empty_queries_and_order_listing(world):
    assert tools.search_products(SHOPPER, " \t\n ")["error"] == "invalid_argument"
    assert tools.find_order(SHOPPER, " \t\n ") == {"ok": True, "orders": []}
    result = tools.list_my_orders(AuthContext(user_id=999_999, role="shopper"))
    assert result == {"ok": True, "orders": [], "count": 0}
    for ctx in (SHOPPER, MERCHANT):
        result = tools.list_my_orders(ctx)
        assert result["count"] == len(result["orders"]) == 20
        keys = [(order["ordered_at"], order["order_id"]) for order in result["orders"]]
        assert keys == sorted(keys, reverse=True)


@pytest.mark.parametrize("ctx", [SHOPPER, MERCHANT, SUPPORT])
def test_hw1_cancellation_checks_scope_before_status(world_copy, ctx):
    result = tools.cancel_order(ctx, 4127, "too late")
    assert result["error"] == "not_eligible"
    assert "delivered" in result["reason"]
    assert "before shipment" in result["reason"]
    strangers = [
        AuthContext(user_id=2, role="shopper"),
        AuthContext(user_id=9002, role="merchant", store_id=2),
    ]
    for stranger in strangers:
        denied = tools.cancel_order(stranger, 4127, "not mine")
        assert denied["error"] == "permission_denied"
        assert stranger.role in denied["reason"]
        assert "4127" in denied["reason"]
        assert "delivered" not in denied["reason"]
    with db.connection() as conn:
        assert db.get_order(conn, 4127).status == "delivered"
        conn.execute("UPDATE orders SET status = 'placed' WHERE id = 4127")
        conn.commit()
    assert tools.cancel_order(ctx, 4127, "changed my mind") == {
        "ok": True,
        "order_id": 4127,
        "status": "cancelled",
    }
    assert tools.cancel_order(ctx, 4127, "again")["error"] == "not_eligible"
    with db.connection() as conn:
        assert db.get_order(conn, 4127).status == "cancelled"


def test_hw1_cancellation_pause_does_not_open_database(monkeypatch):
    monkeypatch.setenv("CARTWHEEL_KILL_SWITCH", "readonly")

    def unexpected_database_access():
        pytest.fail("a paused cancellation must not access the database")

    monkeypatch.setattr(db, "connection", unexpected_database_access)
    result = tools.cancel_order(SHOPPER, 4127, "paused")
    assert result["error"] == "paused"


@pytest.mark.parametrize(
    "ctx,scope",
    [
        (SHOPPER, "shopper"),
        (AuthContext(user_id=9002, role="merchant", store_id=2), "merchant"),
        (SUPPORT, "support"),
    ],
)
def test_hw1_find_order_natural_language(order_search_cases, ctx, scope):
    _, expected = order_search_cases
    result = tools.find_order(ctx, "the UNIQUE item I bought last week?")
    assert [order["order_id"] for order in result["orders"]] == expected[scope][:5]


@pytest.mark.parametrize(
    "role,field,value",
    [
        ("shopper", "user_id", None),
        ("merchant", "store_id", None),
        ("support", "user_id", None),
        ("shopper", "role", "unknown"),
    ],
)
def test_hw1_find_order_rejects_invalid_scope(monkeypatch, role, field, value):
    ctx = AuthContext(user_id=1, role=role, store_id=1 if role == "merchant" else None)
    object.__setattr__(ctx, field, value)

    def unexpected_database_access():
        pytest.fail("an invalid scope must not access the database")

    monkeypatch.setattr(db, "connection", unexpected_database_access)
    result = tools.find_order(ctx, "product")
    assert result["error"] == "permission_denied"
    assert result["reason"]
