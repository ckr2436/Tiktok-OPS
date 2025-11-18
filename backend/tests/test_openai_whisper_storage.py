from __future__ import annotations

from app.features.tenants.openai_whisper import storage


def test_workspace_dir_is_created(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    workspace = storage.workspace_dir(7)
    assert workspace == tmp_path / "workspace_7"
    assert workspace.exists()


def test_upload_metadata_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    payload = {"upload_id": "abc", "foo": "bar"}
    storage.write_upload_metadata(workspace_id=2, upload_id="abc", payload=payload)

    data = storage.load_upload_metadata(workspace_id=2, upload_id="abc")
    assert data["upload_id"] == "abc"
    assert data["foo"] == "bar"
    assert "created_at" in data
    assert "updated_at" in data


def test_delete_upload_removes_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    directory = storage.upload_dir(3, "deadbeef")
    file_path = directory / "upload.ext"
    file_path.write_text("data")

    storage.delete_upload(3, "deadbeef")
    assert not directory.exists()
