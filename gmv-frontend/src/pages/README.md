# GMV Ops Frontend (Vite + React + Redux)

- 接口前缀：`/api/v1`（见 `src/core/config.js`）
- 平台登录：`POST /platform/auth/login`（username + password）
- 会话探测：`GET /platform/auth/session`
- 平台初始化：`GET /platform/admin/exists` + `POST /platform/admin/init`
- 公司成员：`/tenants/{workspace_id}/users[...]`

## 本地起步
```bash
npm i
npm run dev  # http://localhost:5173
```

如需免 CORS，后端跑在 `http://127.0.0.1:8000`，可在 `vite.config.js` 打开代理。
