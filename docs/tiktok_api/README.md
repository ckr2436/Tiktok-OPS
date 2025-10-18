# TikTok Business API Docs (Official-only, Codex-ready)

- 官方目录结构保存在 `pages/` 下（根为 `API Reference/`）。
- 每个端点 1 个 JSON 文件，字段仅包含**官方提供**的 `request`/`response` 数据与示例（**不自动补**）。
- 顶层索引在 `index.json`，包含 id/title/method/endpoint/path/flags。

快速检索：
```bash
python tools/search_api_docs.py adgroup get
```
