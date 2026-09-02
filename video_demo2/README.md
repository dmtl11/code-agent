# 🐍 网页版贪吃蛇

一个不依赖任何外部资源的贪吃蛇小游戏：

- **前端** `frontend/`：原生 HTML / CSS / JavaScript，支持方向键与 WASD 移动、得分、最高分（localStorage）、游戏结束与重新开始。
- **后端** `backend/`：仅用 Python 标准库提供网页与 JSON API，使用 SQLite 保存玩家昵称与得分，并提供排行榜。

## 目录结构

```
.
├── backend/
│   └── server.py        # HTTP 服务器 + SQLite 存储 + API
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── game.js
├── tests/
│   └── test_backend.py  # 后端单元测试与 API 测试
└── README.md
```

## 启动说明

需要 Python 3.7+（仅标准库，无需安装任何依赖）。

```bash
# 从项目根目录启动（默认 127.0.0.1:8000）
python backend/server.py

# 或指定端口 / 地址
PORT=9000 python backend/server.py
HOST=0.0.0.0 PORT=9000 python backend/server.py
```

启动后浏览器访问：<http://127.0.0.1:8000>

首次启动会自动在 `backend/` 下创建 `snake.db`（SQLite 数据库）。

## API

| 方法 | 路径          | 说明                                   |
| ---- | ------------- | -------------------------------------- |
| GET  | `/api/health` | 健康检查，返回 `{"status":"ok"}`       |
| GET  | `/api/scores` | 返回排行榜前 10 名 `{"scores":[...]}`  |
| POST | `/api/scores` | 保存分数，请求体 `{"name":"昵称","score":120}` |

POST 校验：昵称非空、长度 ≤ 20，仅允许字母/数字/下划线/中文/空格/连字符；分数为 0 ~ 1000000 的整数。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

测试使用临时 SQLite 数据库，不会污染真实数据。
