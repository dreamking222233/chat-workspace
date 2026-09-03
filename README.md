# Chat Workspace

一个 OpenAI-compatible 的文本/图片模型工作台，包含 FastAPI 后端和 React + Vite 前端。

## 功能

- 用户注册、登录与管理员授权
- 多模型渠道管理，自动从 `/v1/models` 同步模型
- SSE 流式文本对话、断线重连、重新生成和停止生成
- 图片生成、参考图编辑，以及文本模型的 `generate_image` 工具调用
- 项目、对话归档/删除、Markdown/JSON/TXT 导出
- MySQL 持久化对话和请求记录；图片文件保存到可配置的资产目录

## 目录

```text
src/                 React 前端
backend/app/         FastAPI 应用
backend/alembic/     数据库迁移
backend/tests/       后端测试
docs/                功能说明
md/                  方案、实施与 Review 记录
init.sql             当前数据库结构（仅 schema，无业务数据）
```

## 本地启动

1. 复制 `backend/.env.example` 为 `backend/.env`，设置数据库、JWT、加密密钥和管理员凭据。
2. 创建数据库结构：

```bash
mysql -u root -p < init.sql
```

也可以使用 Alembic：

```bash
cd backend
python -m pip install -e ".[dev]"
alembic upgrade head
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

前端：

```bash
npm install
npm run dev -- --host 0.0.0.0 --port 4180
```

生产环境请将 `CHAT_STORAGE_DIR` 设置为持久化绝对路径，并为该目录配置磁盘或容器卷；图片接口从该目录读取二进制文件，MySQL 只保存资源元数据。

## 配置与密钥

不要提交 `.env`、数据库转储、运行时数据库、上传文件或供应商 API Key。公开仓库只包含 `backend/.env.example`，其中的值均为示例占位符。

## 测试

```bash
cd backend && pytest
npm test
npm run build
```

## 数据库初始化脚本

根目录 `init.sql` 来自当前 MySQL schema，包含 12 张表及索引、外键，标记 Alembic revision `0004_channel_type`。脚本不包含用户、对话、令牌、渠道密钥或图片二进制数据。
