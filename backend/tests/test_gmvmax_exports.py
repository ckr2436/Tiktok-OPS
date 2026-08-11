import importlib


def test_gmvmax_client_exports_store_requests():
    module = importlib.import_module("app.providers.tiktok_business.gmvmax_client")
    required = {
        "GMVMaxStoreListRequest",
        "GMVMaxStoreAdUsageCheckRequest",
        "GMVMaxStoreListData",
        "GMVMaxStoreAdUsageCheckData",
    }

    for name in required:
        assert hasattr(module, name), f"{name} missing on gmvmax_client"
        assert name in getattr(module, "__all__", ()), f"{name} missing from __all__"


def test_provider_package_reexports_store_requests():
    provider_pkg = importlib.import_module("app.providers.tiktok_business")
    required = {
        "TikTokBusinessGMVMaxClient",
        "GMVMaxStoreListRequest",
        "GMVMaxStoreAdUsageCheckRequest",
        "GMVMaxStoreListData",
        "GMVMaxStoreAdUsageCheckData",
    }

    for name in required:
        assert hasattr(provider_pkg, name), f"{name} missing on provider package"
        assert name in getattr(provider_pkg, "__all__", ()), f"{name} missing from __all__"
