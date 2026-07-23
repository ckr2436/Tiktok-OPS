from app.services.hermes_agent import direct_browser


def test_existing_response_does_not_cross_browser_slots(monkeypatch):
    project_id = "cf_project_123"
    response = (
        '{"schema_version":"1.0","project_id":"cf_project_123",'
        '"stage":"CREATIVE","status":"PASS","result":{"concepts":[]},'
        '"next_stage":"VISUAL_PREVIEW"}'
    )
    monkeypatch.setattr(direct_browser, "CDP_URL", "http://127.0.0.1:9222")

    monkeypatch.setattr(
        direct_browser,
        "_list_tabs",
        lambda: [{"tabId": "t1", "active": True, "url": "https://chatgpt.com/"}],
    )

    def page_state(isolated=True):
        if direct_browser.CDP_URL.endswith(":9224"):
            raise AssertionError("recovery must not scan another browser slot")
        return {"busy": False, "messageTexts": ["unrelated response"], "url": "https://chatgpt.com/"}

    monkeypatch.setattr(direct_browser, "_page_state", page_state)
    recovered = direct_browser._existing_stage_response(
        {
            "project_id": project_id,
            "current_stage": "CREATIVE",
            "browser_cdp_url": "http://127.0.0.1:9222",
        }
    )

    assert recovered is None
    assert direct_browser.CDP_URL == "http://127.0.0.1:9222"


def test_existing_response_scans_tabs_inside_browser_slot(monkeypatch):
    project_id = "cf_project_tabs"
    response = (
        '{"schema_version":"1.0","project_id":"cf_project_tabs",'
        '"stage":"CREATIVE","status":"PASS","result":{"concepts":[]},'
        '"next_stage":"VISUAL_PREVIEW"}'
    )
    monkeypatch.setenv("HERMES_BROWSER_SLOTS", "1")
    monkeypatch.setattr(direct_browser, "CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(
        direct_browser,
        "_list_tabs",
        lambda: [
            {"tabId": "t1", "active": True, "url": "https://chatgpt.com/"},
            {"tabId": "t2", "active": False, "url": "https://chatgpt.com/c/recovered"},
        ],
    )
    active = {"tab": "t1"}

    def activate(tab_id):
        active["tab"] = tab_id
        return True

    def page_state(isolated=True):
        if active["tab"] == "t2":
            return {"busy": False, "messageTexts": [response], "url": "https://chatgpt.com/c/recovered"}
        return {"busy": False, "messageTexts": ["unrelated"], "url": "https://chatgpt.com/"}

    monkeypatch.setattr(direct_browser, "_activate_tab", activate)
    monkeypatch.setattr(direct_browser, "_page_state", page_state)

    recovered = direct_browser._existing_stage_response(
        {
            "project_id": project_id,
            "current_stage": "CREATIVE",
            "browser_cdp_url": "http://127.0.0.1:9222",
        }
    )

    assert recovered == (response, "https://chatgpt.com/c/recovered")
    assert active["tab"] == "t1"
