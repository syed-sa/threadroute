import pytest

from threadroute.domain import wms
from threadroute.domain.wms import WmsError


def test_seed_counts():
    assert len(wms.list_locations()) == 10
    assert len(wms.list_shipments(limit=50)) == 35
    assert wms.get_sku("TR-CHN-KHK")["sizes"] == ["30", "32", "34", "36", "38"]


def test_story_shipment_is_delayed_for_monsoon():
    s = wms.get_shipment("sh-1001")
    assert s["status"] == "delayed" and s["days_late"] == 4
    assert "NH44" in s["hold_reason"] and s["total_units"] == 400 and s["value_at_risk"] > 0


def test_unknown_ids_raise_structured_errors():
    with pytest.raises(WmsError) as exc:
        wms.get_shipment("SH-9999")
    assert exc.value.code == "SHIPMENT_NOT_FOUND"


def test_low_stock_and_donors():
    low = wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]
    assert low["qty"] == 0 and low["stockout_in_days"] == 0
    donors = wms.find_donor_stores("SH-OXF-WHT", "L", "ST-BLR-IND", 40)["donors"]
    assert donors[0]["location_id"] == "ST-HYD-BJH" and donors[0]["can_cover_full_need"]
    assert all(d["transferable_surplus"] > 0 for d in donors)


def test_reallocation_respects_safety_cover():
    with pytest.raises(WmsError) as exc:
        wms.reallocate_stock("ST-HYD-BJH", "ST-BLR-IND", "SH-OXF-WHT", "L", 50)
    assert exc.value.code == "INSUFFICIENT_SURPLUS"
    res = wms.reallocate_stock("ST-HYD-BJH", "ST-BLR-IND", "SH-OXF-WHT", "L", 20, "test")
    assert res["ok"] and res["transfer_id"].startswith("TRF-")
    assert wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]["qty"] == 20
    assert wms.get_inventory(location_id="ST-HYD-BJH", sku="SH-OXF-WHT", size="L")[0]["qty"] == 32


def test_reallocation_rejects_bad_input():
    for args, code in [(("A", "A", "SH-OXF-WHT", "L", 1), "SAME_LOCATION"),
                       (("ST-HYD-BJH", "ST-BLR-IND", "SH-OXF-WHT", "L", 0), "INVALID_QTY"),
                       (("NOPE", "ST-BLR-IND", "SH-OXF-WHT", "L", 1), "LOCATION_NOT_FOUND")]:
        with pytest.raises(WmsError) as exc:
            wms.reallocate_stock(*args)
        assert exc.value.code == code


def test_claim_value_and_validation():
    res = wms.create_claim("SH-1002", "damage", "crushed_carton", "moderate", 12, {})
    assert res["claim_id"] == "CL-2001" and res["claim_value"] == 34800.0 and res["carrier"] == "RoadRunner Express"
    assert wms.list_claims("SH-1002")[0]["status"] == "filed"
    with pytest.raises(WmsError) as exc:
        wms.create_claim("SH-1002", "damage", "crushed_carton", "moderate", 9999)
    assert exc.value.code == "INVALID_UNITS"
    with pytest.raises(WmsError):
        wms.create_claim("SH-1002", "theft", "other", "minor", 1)


def test_reroute_rules():
    with pytest.raises(WmsError) as exc:
        wms.reroute_shipment("SH-1001", "road", "AirLink Cargo")
    assert exc.value.code == "MODE_NOT_SUPPORTED"
    with pytest.raises(WmsError) as exc:
        wms.reroute_shipment("SH-1002", "air", "AirLink Cargo")  # already delivered
    assert exc.value.code == "NOT_REROUTABLE"
    res = wms.reroute_shipment("SH-1001", "air", "AirLink Cargo", "monsoon")
    s = wms.get_shipment("SH-1001")
    assert res["ok"] and s["carrier"] == "AirLink Cargo" and s["status"] == "in_transit" and s["hold_reason"] is None


def test_return_grading_rules():
    with pytest.raises(WmsError) as exc:
        wms.set_return_disposition("RT-9001", "A", "liquidate")
    assert exc.value.code == "GRADE_DISPOSITION_MISMATCH"
    before = wms.get_inventory(location_id="DC-GGN", sku="JK-BMB-OLV", size="M")[0]["qty"]
    wms.set_return_disposition("RT-9001", "A", "restock", "like new")
    assert wms.get_inventory(location_id="DC-GGN", sku="JK-BMB-OLV", size="M")[0]["qty"] == before + 1
    assert wms.get_return("RT-9001")["status"] == "graded"
