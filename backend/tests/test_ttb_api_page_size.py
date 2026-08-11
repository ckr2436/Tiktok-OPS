from app.services.ttb_api import _clamp_page_size, _page_size_limit


def test_gmvmax_report_keeps_documented_large_page_size() -> None:
    limit = _page_size_limit("/gmv_max/report/get/")

    assert limit == 1000
    assert _clamp_page_size(200, default=limit, maximum=limit) == 200
    assert _clamp_page_size(5000, default=limit, maximum=limit) == 1000


def test_website_ads_report_keeps_documented_large_page_size() -> None:
    limit = _page_size_limit("/report/integrated/get/")

    assert limit == 1000
    assert _clamp_page_size(5000, default=limit, maximum=limit) == 1000


def test_standard_ad_list_keeps_documented_large_page_size() -> None:
    limit = _page_size_limit("/ad/get/")

    assert limit == 1000
    assert _clamp_page_size(5000, default=limit, maximum=limit) == 1000


def test_website_ads_identity_keeps_documented_hundred_rows() -> None:
    limit = _page_size_limit("/identity/get/")

    assert limit == 100
    assert _clamp_page_size(200, default=limit, maximum=limit) == 100


def test_store_product_endpoint_keeps_documented_hundred_rows() -> None:
    limit = _page_size_limit("/store/product/get/")

    assert limit == 100
    assert _clamp_page_size(200, default=limit, maximum=limit) == 100
    assert _clamp_page_size(0, default=limit, maximum=limit) == 1


def test_other_tiktok_endpoints_remain_capped_at_fifty() -> None:
    limit = _page_size_limit("/store/list/")

    assert limit == 50
    assert _clamp_page_size(200, default=limit, maximum=limit) == 50


def test_gmvmax_campaign_list_keeps_documented_hundred_rows() -> None:
    limit = _page_size_limit("/gmv_max/campaign/get/")

    assert limit == 100
    assert _clamp_page_size(200, default=limit, maximum=limit) == 100


def test_ad_video_search_keeps_documented_hundred_rows() -> None:
    limit = _page_size_limit("/file/video/ad/search/")

    assert limit == 100
    assert _clamp_page_size(200, default=limit, maximum=limit) == 100
