from __future__ import annotations

import asyncio
import json

from app.services.website_ads_targeting_catalog import (
    catalog_path,
    load_targeting_catalog,
    match_general_interest_ids,
    normalize_targeting_catalog,
    record_targeting_discovery,
    sync_targeting_catalog,
)


def _official_payload():
    return {
        "code": 0,
        "data": {
            "interest_categories": [
                {
                    "interest_category_id": "10",
                    "interest_category_name": "Health and wellness",
                    "level": 1,
                    "sub_category_ids": ["101"],
                    "placements": ["PLACEMENT_TIKTOK"],
                },
                {
                    "interest_category_id": "101",
                    "interest_category_name": "Sleep and relaxation",
                    "level": 2,
                    "sub_category_ids": [],
                    "placements": ["PLACEMENT_TIKTOK"],
                },
            ]
        },
    }


def _search_payload():
    return {
        "code": 0,
        "request_id": "req-1",
        "data": {
            "general_interest": {
                "list_result": [
                    {
                        "sub_targeting_type": "GENERAL_INTEREST",
                        "id": "101",
                        "name": "Sleep & Relaxation",
                        "level": 2,
                        "children_ids": [],
                    }
                ]
            },
            "purchase_intention": {
                "list_result": [
                    {
                        "sub_targeting_type": "PURCHASE_INTENTION",
                        "id": "p-1",
                        "name": "Wellness products",
                        "level": 1,
                        "children_ids": [],
                    }
                ]
            },
        },
    }


def test_normalize_catalog_merges_official_sources_and_builds_parents():
    catalog = normalize_targeting_catalog(
        advertiser_id="advertiser-1",
        language="en",
        interest_categories_payload=_official_payload(),
        targeting_search_payload=_search_payload(),
    )
    assert catalog["counts"] == {"GENERAL_INTEREST": 2, "PURCHASE_INTENTION": 1}
    by_id = {(item["targeting_type"], item["id"]): item for item in catalog["categories"]}
    assert by_id[("GENERAL_INTEREST", "101")]["name"] == "Sleep & Relaxation"
    assert by_id[("GENERAL_INTEREST", "101")]["parent_ids"] == ["10"]
    assert catalog["raw_responses"]["interest_category_v2"]["code"] == 0


def test_catalog_sync_writes_versioned_json_and_local_matcher_uses_it(tmp_path):
    class Api:
        async def list_interest_categories(self, *args, **kwargs):
            return _official_payload()

        async def search_targeting(self, *args, **kwargs):
            return _search_payload()

    result = asyncio.run(sync_targeting_catalog(Api(), "advertiser-1", root=tmp_path))
    assert result["counts"]["GENERAL_INTEREST"] == 2
    path = catalog_path("advertiser-1", root=tmp_path)
    assert path.is_file()
    assert load_targeting_catalog("advertiser-1", root=tmp_path)["schema_version"] == 1
    assert match_general_interest_ids(
        "advertiser-1", ["sleep relaxation"], root=tmp_path
    ) == ["101"]


def test_keyword_discovery_is_persisted_and_reused(tmp_path):
    payload = {
        "request_id": "req-2",
        "data": {
            "general_interest": {
                "search_result": {
                    "night routine": [
                        {
                            "sub_targeting_type": "GENERAL_INTEREST",
                            "id": "night-1",
                            "name": "Night routines",
                        }
                    ]
                }
            }
        },
    }
    record_targeting_discovery(
        "advertiser-1", "night routine", payload, root=tmp_path
    )
    assert match_general_interest_ids(
        "advertiser-1", ["night routine"], root=tmp_path
    ) == ["night-1"]
    saved = json.loads(
        (tmp_path / "advertiser-1" / "hermes_discoveries_en.json").read_text(encoding="utf-8")
    )
    assert saved["keywords"]["night routine"]["verification_count"] == 1
