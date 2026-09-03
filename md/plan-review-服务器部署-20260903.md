# 服务器部署-20260903 方案评估

## 结论

方案可实施，采用 Git + Python venv + systemd/Uvicorn + Nginx，并将 MySQL 与资产目录独立持久化，适合当前 FastAPI/React 项目。实施前需以服务器现状检查结果为准，避免覆盖既有服务和数据库。

## 评估与调整建议

1. **仓库与版本**：先确认 `main` 可克隆，再记录部署前 commit；更新部署采用临时目录/备份后原子切换，避免半更新状态。
2. **权限与网络**：SSH 登录后检查 sudo、GitHub 出站、DNS、80/443/8000 端口；若服务器没有 sudo 或出站受限，应停止在对应步骤并记录原因。
3. **数据库**：优先复用现有 MySQL，执行备份后用 Alembic 升级；仅在明确没有数据库时启用 Docker MySQL。不要将生产密钥写入仓库或 shell 历史。
4. **运行时**：锁定 Python/Node 版本并使用虚拟环境；前端构建产物由 Nginx 提供，API 与 SSE 代理需关闭缓冲并设置足够超时。
5. **资产与安全**：`CHAT_STORAGE_DIR` 使用持久化绝对路径并限制权限；生产 `.env` 仅服务器保存，设置随机 JWT/Fernet 密钥、CORS 为实际域名，关闭开发热重载。
6. **验证与回滚**：先执行本地测试结果核对，再检查 `/api/health`、登录、模型列表、SSE 与图片读写；保留旧目录、服务单元和数据库备份，失败时恢复上一 commit。

## 发布门槛

- `npm test -- --run`：49 passed；`npm run build`：通过。
- `PYTHONPATH=backend pytest -q backend/tests`：151 passed，1 skipped。
- `git diff --check` 通过，公开提交不包含 `.env`、运行时数据库、上传资产或 API 密钥。
