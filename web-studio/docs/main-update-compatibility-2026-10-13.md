# Web Studio 合并主干后的双版本功能处理建议

核对日期：2026-10-13。主干基准：`d41cd6db1`；合并结果：`b4f26873c`；已有双 Provider 实现：`75945fa24`。本报告补充 [原接口审计](./api-contract-audit.md)，依据当前源码和当天读取的火山官方文档。Gateway 和启动配置的前端限制已补齐，详见下方实施状态；本次没有访问真实实例或执行写入操作。

## 实施状态

- 已统一 Gateway 导航、深链接和请求层的开源 api_key 管理权限限制；禁止云端、unknown、trusted、dev 或未验证连接请求 Gateway 管理接口，手动重试也受限制。
- 已将启动配置限定为开源 ROOT；在服务类型、身份或授权变化时返回连接页并卸载编辑器。编辑器的读、预览、保存及重启也检查授权。
- 新增中英文 trusted 模式说明。Filesystem 的开源扩展定位和 Working Memory 的版本边界维持原有处理。
- 下述“当前前端缺口”记录本次修复前的状态，Gateway 登记、Provider 判断、认证模式限制及启动配置挂载限制均已修复。云端 Gateway 和 TOP 管理适配仍未实现。

## 结论

新增管理功能不能随合并自动开放到火山连接。继续沿用「服务类型、认证模式、授权、具体接口契约」共同决定入口的方式，不增加独立能力接口。

| 主干新增或调整 | 开源连接 | 火山连接 | 处理建议 |
|---|---|---|---|
| Gateway：上游、策略、Key、日志、接入指引 | 当前适合 `api_key` 模式下有独立管理凭证的 admin/root；还需确认 Gateway 实际开启 | 未确认托管版对应管理契约 | 开源管理域；云端隐藏并拦截深链接；暂不对 trusted 开放签发/接入 |
| 启动配置：模型表单、配置文件、保存、重启 | ROOT 专属；文件可写及服务重启能力由响应决定 | 云库配置属于另一个管理对象及授权面 | 开源 ROOT 展示；其他连接不挂载编辑器；火山提供控制台管理入口 |
| Playground 更名 Filesystem | 保留完整页面及旧链接重定向 | 当前完整页面仍包含未适配的 Bot、终端和会话操作 | 保持开源扩展定位；云端继续使用既有目录页 |
| trusted 强制 Root Key、停止签发个人 Key | 更新凭证提示及身份切换逻辑，不能继续视为无 Key 模式 | 沿用 Bearer User Key；不能照搬 trusted | 认证模式与 Provider 分开处理；新 trusted 与 Gateway 用户绑定需单独验证 |
| Working Memory 默认关闭 | 采用主干默认；不影响消息归档、长期记忆抽取 | 官方创建会话文档仍写默认开启 | 不共享默认值；不向云端透传未确认的新 Commit 参数 |
| 聊天布局、任务耗时、品牌图标、Compile 缺省来源修复 | 合并保留 | 可共用展示调整；Bot/Compile 原有门禁继续保留 | 展示变化不增加接口能力；缺字段保留已有兼容处理 |

## Gateway 的接口与处理边界

Studio 调用的管理入口为 `/api/v1/admin/gateway`：

| 能力 | 调用 |
|---|---|
| 接入与可用性 | `GET /guides` |
| 总览、日志、工具 | `GET /overview`、`GET /logs`、`GET /tools` |
| 上游 | `GET /upstreams`、`PUT/DELETE /upstreams/{id}`、`POST /upstreams/{id}/test` |
| 策略 | `GET /policies`、`PUT/DELETE /policies/{id}` |
| Key | `GET/POST /keys`、`DELETE /keys/{id}` |
| 重新开始捕获 | `POST /keys/{id}/capture/reset` |
| 清除 Gateway 用户数据 | `DELETE /users/{user_id}/data` |

用户选择另外复用 `GET /api/v1/admin/accounts/{account_id}/users?include_credentials=false`。服务端从已认证上下文决定账户，浏览器不直接获得内部 Gateway 管理令牌。相关源码：[前端 API](../src/routes/gateway/-lib/api.ts)、[服务端代理](../../openviking/server/routers/gateway.py)。

需要区分四类凭证：Studio 的管理 API Key、服务端代理的 Gateway 管理令牌、客户端使用的 Gateway Key、上游模型 API Key。上游支持火山方舟模型，不意味着 Gateway 已支持火山托管 OpenViking 数据库。

云端官方数据面仍要求 `Authorization: Bearer`；当前 Gateway 数据客户端对 HTTP/MCP 使用 `X-API-Key`，还依赖健康检查身份、会话回流、context 搜索等契约。仅改请求头不足以证明兼容。此次未定位到托管版 `/admin/gateway` 公开契约，应标为「未确认」。来源：[云端鉴权](https://docs.volcengine.com/docs/vector_database_vikingdb/APIAuthenticationInstructions?lang=zh)、[Gateway 客户端](../../openviking_gateway/client.py)。

### 当前前端缺口

1. `/gateway` 未注册到 [STUDIO_FEATURES](../src/lib/studio-service.ts)，全局页面边界无法按管理域限制它。
2. [useGateway](../src/routes/gateway/-lib/use-gateway.ts) 调用管理权限函数时没有传 `serviceProvider`，而函数默认开源。云端导航虽隐藏，但深链接在存在旧管理 Key、角色符合条件时仍可能请求开源管理接口。
3. Gateway 现在复用 `canManageUsers`，没有限制 trusted。新的 trusted 仅接受 Root Key，并依赖身份头；Gateway 只传 Key，签发需要解析用户身份，且明确拒绝 root 身份。个人 Key 已停止签发，现有用户绑定流程不能当作兼容 trusted。

建议用现有权限模块增加前端 `canManageGateway` 规则：服务 ready、Provider 为 opensource、模式为 api_key、已验证管理凭证及角色为 admin/root。导航、页面边界、查询和动作共享这条规则。用现有 `GET /guides` 处理未开启、旧版本、管理令牌错误、服务不可达；不要用写操作或上游模型测试做自动能力探测。

ROOT 管理者可管理 Gateway，但签发时应绑定独立的用户/admin 数据 Key，不能把 Root Key当作用户数据 Key。清除用户数据只清理 Gateway 侧会话及其 Key，不应描述为删除 OpenViking 正本记忆。[签发与删除实现](../../openviking_gateway/app.py)、[trusted 鉴权](../../openviking/server/auth/plugins/trusted.py)。

## 启动配置与云库模型配置

当前新页面使用：

- `GET /api/v1/admin/configuration?source=file&account_id=…`：读启动配置、revision、模型、文件状态、覆盖项和重启状态。
- `PATCH /api/v1/admin/configuration?source=file&dry_run=true`：预览并验证完整文件及表单修改。
- `PATCH /api/v1/admin/configuration?source=file`：提交完整 content 与 revision。
- `POST /api/v1/admin/restart`：根据 revision 请求重启，返回 202。

这些接口是 ROOT 管理的服务端启动文件操作。重启依赖服务启动方式；保存不等于已重启，202 不等于重启完成。应保留 `writable`、`form_readonly`、`restart.supported` 和实例 ID 变化检查。[前端 API](../src/routes/settings/-lib/config-file-api.ts)、[ROOT 接口](../../openviking/server/routers/admin.py)。

火山公开 `GetOpenVikingCollection` 也有 VLM/Embedding 信息，但需要 AK/SK 和 ResourceID，返回的是云库模型配置，API Key 脱敏。它不是完整 ov.conf，不具有当前编辑器的文件 revision/预览/重启协议。云控制台已有模型凭证管理体验；此次未确认与 ov.conf 编辑同等的公开更新接口。来源：[查询库详情](https://docs.volcengine.com/docs/84313/2685089)、[数据库管理](https://docs.volcengine.com/docs/84313/2486487)。

### 当前前端缺口

[设置路由](../src/routes/settings/route.tsx) 对所有 Provider 展示配置 Tab，并以 `keepMounted` 挂载编辑器。[编辑器](../src/routes/settings/-components/server-configuration.tsx) 仅判断 root 与认证模式，缺少 Provider 门禁。普通云用户一般不会触发读取，但仍看到无意义的 ROOT 提示；不能因此要求用户寻找云端 Root Key。

建议仅在开源、管理身份已确认且为 ROOT 时显示并挂载配置编辑器；编辑器查询和保存/重启动作也重复检查授权。切换 Provider 后回到连接页并卸载编辑器。火山连接保留现有连接设置和云管理说明；需要云库管理时另做 TOP 管理适配，不复用数据 Key、不把 AK/SK 加入当前连接表单。

## Filesystem 更名

完整 Filesystem 仍包含 Agent 面板、Bot 健康检查、上传、终端命令、Session Commit/Extract、归档及工具结果读取。基础目录 API 可兼容不表示整页可兼容。[Agent 面板](../src/routes/filesystem/-components/agent-panel.tsx)、[终端](../src/routes/filesystem/-components/terminal-panel.tsx)。

合并结果已将功能登记、导航、全高布局及连接重置目标改为 `/filesystem`，保留 `/playground` 重定向。保持云端不显示技能/检索详情中的 Filesystem 入口；继续用 `/directory` 的只读浏览，不复制一个云专用完整 Filesystem 页面。

## Working Memory 与会话参数

主干新增 Commit `enable_working_memory`，省略时继承策略；用户记忆预设默认关闭 Working Memory。当前 Studio 的 `commitSession()` 只发送可选 `keep_recent_count`，尚未增加这个参数，不需要为此次合并自动新增开关。[主干 Commit 模型](../../openviking/server/routers/sessions.py)、[Studio 提交调用](../src/lib/sessions/api.ts)、[预设](../src/lib/user-memory-policy.ts)。

当天完整读取的云端 [create_session](https://docs.volcengine.com/docs/84313/2487445) 将 `working_memory.enabled` 默认写为 true；[commit_session](https://docs.volcengine.com/docs/84313/2487441) 没列出 `enable_working_memory`。这是文档契约差异，不能据此断言真实实例一定不支持。云端继续沿用服务默认，不将开源预设的 false 自动写入云会话；未来增加明确控制时需分别适配并联调。

Working Memory 关闭不等于关闭消息归档或长期记忆抽取。归档详情不能假定一定有摘要，缺少 Working Memory 不应被提示为处理失败。原有云端归档及会话写操作边界继续有效，主干新字段不能扩大云端开放范围。

## 建议实施顺序与验收

1. 补 Gateway 统一权限规则、功能登记、深链接限制，并明确 trusted 暂不支持的提示；云端旧管理 Key 不能触发 `/admin/gateway/*` 请求。
2. 补启动配置 Provider/ROOT 门禁。云端、unknown、普通用户连接不挂载编辑器；开源 ROOT 保留读写和有条件重启。
3. 回归服务切换与 Filesystem 旧链接；确认无旧配置草稿、授权或查询缓存跨身份复用。
4. 保留 Working Memory 的版本差异。仅在产品需要时扩展会话控制，不为主干新增字段增加无实际调用的接口或适配器。

必要测试覆盖：云端深链接及遗留管理 Key、开源 ROOT/admin/user、api_key/trusted/dev、Gateway 未开启/不可达、配置只读文件与不支持重启、开源与云端切换、Filesystem 重定向和缺少 Working Memory 的归档。接口写入及真实普通 User 隔离仍需要独立实例验证。
