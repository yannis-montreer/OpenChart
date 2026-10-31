"""Does applying a layout keep the chart's indicator instances' uids? (indicator alerts design,
docs/designs/indicator-alerts.md section 5; answer in docs/decisions-layout-instance-uids.md)

An indicator alert will name its instance by (source, instrument, uid). These tests pin what
`POST /api/layouts/{id}/apply` does today: the chart gets **the layout's** uids. The chart's
own uid survives only where the layout's copy has the same uid, which is the case when the
layout was saved from this chart and the instance was there then.
"""

from tests.test_chart_indicators import cc, chart, ok, put  # noqa: F401  (the fixture)


def save_layout(c, name: str, **layout) -> str:
    r = c.post("/api/layouts", json={"name": name, "layout": layout})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def apply(c, layout_id: str, instrument: str | None = None) -> dict:
    q = f"?instrument={instrument}" if instrument else ""
    return ok(c.post(f"/api/layouts/{layout_id}/apply{q}"))["chart"]


def uids(body: dict) -> list[str]:
    return [i["uid"] for i in body["indicators"]]


def test_a_layout_saved_from_the_chart_keeps_its_uids_on_apply(cc):  # noqa: F811
    put(cc, [{"id": "ema", "uid": "a1"}, {"id": "rsi", "uid": "a2"}])
    layout = save_layout(cc, "Gold", instrument="XAU_USD")  # copies the chart's list
    put(cc, [{"id": "ema", "uid": "a1", "params": {"length": 50}}, {"id": "rsi", "uid": "a2"}])
    body = apply(cc, layout)
    assert uids(body) == ["a1", "a2"]  # kept: the copy has the same uids
    assert body["indicators"][0]["params"]["length"] == 9  # with the layout's values


def test_an_instance_added_after_the_layout_was_saved_is_removed_by_apply(cc):  # noqa: F811
    put(cc, [{"id": "ema", "uid": "a1"}])
    layout = save_layout(cc, "Gold", instrument="XAU_USD")
    put(cc, [{"id": "ema", "uid": "a1"}, {"id": "sma", "uid": "a3"}])
    assert uids(apply(cc, layout)) == ["a1"]  # a3 is gone (its alert would stop)


def test_the_same_indicator_from_another_layout_gets_the_layouts_uid(cc):  # noqa: F811
    """Same definition, same values, other uid: the chart's instance is replaced."""
    layout = save_layout(cc, "Mine", indicators=[{"id": "ema", "uid": "L1"}])
    put(cc, [{"id": "ema", "uid": "a1"}])
    assert uids(apply(cc, layout, "XAU_USD")) == ["L1"]


def test_a_layout_without_uids_gets_them_once_when_saved(cc):  # noqa: F811
    layout = save_layout(cc, "Mine", indicators=[{"id": "ema"}, {"id": "rsi"}])
    first = uids(apply(cc, layout, "XAU_USD"))
    assert len(set(first)) == 2
    assert uids(apply(cc, layout, "XAU_USD")) == first  # applied again: the same uids


def test_one_layout_on_two_charts_gives_both_the_same_uids(cc):  # noqa: F811
    """A uid is unique per chart, not per server: an alert must name the chart too."""
    layout = save_layout(cc, "Mine", indicators=[{"id": "ema", "uid": "L1"}])
    assert uids(apply(cc, layout, "XAU_USD")) == uids(apply(cc, layout, "EUR_USD")) == ["L1"]
    assert uids(chart(cc, "XAU_USD")) == uids(chart(cc, "EUR_USD")) == ["L1"]
