# Chat 图片上传识别自审记录

- 日期：20260903
- 审查对象：`md/plan-chat图片上传识别-20260903.md`、`md/impl-chat图片上传识别-20260903.md`及当前实现
- 审查范围：`/chat` 普通文本模式图片上传、视觉编码、文本模型路由、Provider 协议转换、资产归属与持久化边界、回归测试

## 结论

**通过，无发布阻断项。** 用户在普通文本模式选择图片后，前端会先上传资产并读取当前 `File`，再按视觉输入限制压缩为 JPEG/PNG Data URL；请求通过 `media_inputs[]` 发送到文本流接口。服务端校验请求格式、图片签名、资产归属、渠道能力与大小限制，将像素只在当前请求内挂到最后一条用户消息，分别转换为 Chat Completions 的 `image_url` 和 Responses 的 `input_image`，数据库消息及流事件不保存 Base64 像素。图片生成/编辑仍使用原有图片接口。

## 审查发现（按严重性）

### P0/P1

未发现会阻断普通文本识图链路的问题。

### P2（已确认的边界）

1. 历史消息只持久化资产 ID；刷新后重新生成若要再次让模型读取旧图，需要浏览器从资产重新编码。当前首轮上传请求已携带完整视觉 Data URL，网络重连由幂等/事件回放处理。
2. 当前 Provider 抽象限定为 OpenAI-compatible；Chat Completions 与 Responses 已覆盖，其他协议不在本项目范围。
3. 上传接口保留既有 15 MiB 文件限制；视觉编码器另有 20 MiB 原图保护和单张/整轮编码限制，实际上传上限仍以服务端接口为准。

## 核对结果

- 前端文本模式与图片模式分流，普通文本图片不会调用图片生成接口。
- 自动模型模式下，若线程历史模型不具备视觉能力，会回退到优先级最高的视觉文本渠道；显式模型/渠道保持严格路由。
- 自动路由按渠道逐项检查，即使同一模型 ID 同时存在于文本专用和视觉渠道，也会选择后者。
- `TextMediaInput` 校验 Data URL、Base64、JPEG/PNG 文件头、MIME 一致性、单张和总编码量；服务端再次校验资产拥有者、图片类型、模型张数/MIME/解码字节数。
- `_provider_messages` 仅将视觉部件注入当前请求的最后一条用户消息，并保留脱敏的资产 ID 清单供图片工具使用。
- Chat Completions 发送 `image_url` + `text`；Responses 发送 `input_image` + `input_text`，默认 `store: false`；工具调用、SSE、幂等和停止流程未改变。
- 用户消息响应保留资产 ID，缩略图使用鉴权请求；消息、`content_json` 和 `events_json` 未写入视觉 Base64。
- 运行时资产目录在首次上传时自动创建，避免新部署挂载目录尚未创建导致上传失败。

## 测试与验证

- `npm test -- --run`：9 个测试文件，49 passed。
- `TZ=America/Los_Angeles npm test -- --run` 与 `TZ=Pacific/Auckland npm test -- --run`：均为 49 passed。
- `npm run build`：通过，生成 `dist/assets/index-DEQJxZdf.js`、`dist/assets/index-9OrE7TRc.css`。
- `PYTHONPATH=backend pytest -q backend/tests`：151 passed，1 skipped。
- `PYTHONPATH=backend python -m compileall -q backend/app backend/tests`：通过。
- `backend/tests/test_multimodal_chat.py`：12 passed，覆盖协议 payload、视觉路由回退、共享模型 ID 路由、模型限制、资产归属、脱敏持久化和缺失目录上传。

## 建议

后续若需要“历史图片重新生成”体验，可在重新生成请求中从持久化资产重新读取并按同一浏览器编码器生成 `media_inputs[]`；不应把原始 Base64 写入消息或事件日志。
