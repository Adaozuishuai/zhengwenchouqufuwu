# 正文抽取服务（Extract Service）

一个基于 FastAPI 的网页抓取与“正文原文”抽取服务，用于把输入的网页 URL 抓取并抽取成适合导出为 PDF 的纯文本正文。

## 功能

- 输入 URL（支持 query / JSON / 纯文本对话式），抓取页面并抽取主内容正文
- 输出严格 JSON：
  - 成功：`{"url":"<输入url>","title":"<标题>","text":"<正文>"}`
  - 失败：`{"detail":"<错误原因>"}`
- 安全：仅允许公网 `http/https` URL，拒绝 `file://`、`localhost`、内网/回环/保留 IP（包含域名解析到内网 IP 的情况）
- 跟随重定向（默认最多 5 次）
- 尊重超时（默认 15 秒，最大 60 秒）
- 页面依赖 JS 渲染时：若环境安装了 Playwright（Chromium），优先无头渲染；否则降级为直接抓取 HTML

## HTTP 接口

### 健康检查

- `GET /health`
- 响应：`{"ok": true}`

### 正文抽取

- `POST /extract`
- 认证（可选）：若设置了环境变量 `EXTRACT_API_KEY`，则请求头必须携带 `x-api-key: <key>`

#### 输入方式 1：Query 参数

`POST /extract?url=https://example.com&timeout=15`

#### 输入方式 2：JSON Body

`POST /extract`，Body：

```json
{"url":"https://example.com","timeout":15}
```

#### 输入方式 3：纯文本（对话式）

`POST /extract`，Body（`url=` / `timeout=` 形式，URL 可用反引号或引号包裹）：

```
url=`https://example.com` timeout=15
```

## 运行

### 方式 1：Uvicorn 启动

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

### 方式 2：PM2 启动（可选）

项目包含 [ecosystem.config.cjs](file:///opt/extract-service/ecosystem.config.cjs)：

```bash
pm2 start ecosystem.config.cjs
pm2 logs extract-api
```

## 环境变量

- `EXTRACT_API_KEY`：不为空时启用鉴权，请求头需携带 `x-api-key`
- `EXTRACT_MAX_REDIRECTS`：最大重定向次数（默认 `5`）
- `EXTRACT_MAX_HTML_BYTES`：抓取 HTML 最大字节数（默认 `5000000`）

## 错误返回约定

- URL 非法/不允许：`{"detail":"Invalid url"}`
- 超时：`{"detail":"timeout"}`
- 抓取失败：`{"detail":"fetch failed: <简短原因>"}`
- 抽取失败：`{"detail":"extract failed"}`

## 示例（curl）

```bash
curl -sS -X POST 'http://127.0.0.1:8002/extract?url=https://example.com&timeout=15'
```

```bash
curl -sS -X POST 'http://127.0.0.1:8002/extract' \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com","timeout":15}'
```

```bash
curl -sS -X POST 'http://127.0.0.1:8002/extract' \
  -H 'content-type: text/plain; charset=utf-8' \
  --data 'url=`https://example.com` timeout=15'
```
