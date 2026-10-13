# Web Studio 双版本兼容与 UI 范围

核对日期：2026-10-13。主干基准：`d41cd6db1`。本次实现限定在 `web-studio/`；真实火山实例未重新联调。

## 界面原则

目标是在同一套 Web Studio 中使用开源或火山引擎 API Key。保持最新 main 的侧栏、页面名称、布局和已有操作，只按接口契约与权限显示功能。耦合模块通过页面内的 Tab 隔离，不重新划分整套产品导航。

- 移除“工作区／高级功能／管理”顶部全局切换。
- 保留 main 的工作区、运维侧栏分组和菜单名称；开源连接保留原有导航。
- 移除独立“我的目录”页面；旧 `/directory` 地址跳转到 `/filesystem`，旧记忆地址仍定位对应目录。
- 文件系统复用已有目录树、文件预览和搜索组件。云端增加个人空间／共享资源 Tab；开源仍保留原来的 Agent／终端 Tab 和操作面板。云端不挂载本地 Bot、终端与未适配写操作。
- 开源默认进入原首页；火山默认进入共享文件系统界面。不因 Key 不同更换整体 UI。
- 恢复任务页统计及会话、监控页面原有布局；保留接口参数修正、缺字段容错和云端操作限制。

## 功能与授权

| 模块 | 开源 API Key | 火山 API Key |
|---|---|---|
| 文件浏览、预览、检索、技能 | 原有界面和操作 | 使用已对齐读取接口；检索与技能详情可跳转文件系统；预览只读 |
| 文件系统 Agent／终端 Tab、VikingBot、Compile、经验分析 | 保留 | 不展示、不挂载未适配模块 |
| 会话 | 原列表、聊天、历史与记忆影响 | 现有上下文记录；不请求未确认的归档接口，不挂载 Bot 输入与记忆影响 |
| 任务 | 原表格和统计；重试 URL 参数修正为 path | 已适配数组列表；不假定 include_events、游标和缺省字段一致 |
| Watches、日志、监控 | 保留原功能 | 按读取响应能力启用；Watches 写操作隐藏 |
| 用户管理、OAuth | 原有管理凭证与角色判断 | 原生管理与 OAuth 隐藏；数据 Key 不授予管理能力 |
| Gateway | api_key 模式且已验证 admin/root 管理凭证 | 隐藏导航、拦截深链接与请求 |
| 启动配置与重启 | ROOT 专属；文件可写及重启能力由响应决定 | 隐藏配置 Tab，不挂载编辑器，不调用开源配置接口 |

Gateway 暂不对 trusted 开放：主干 trusted 仅接受 Root Key并停止签发个人 Key；现有 Gateway 绑定依赖用户 Key且不发送 trusted 身份头。无需新增能力接口，继续使用现有权限判断及 Gateway guides 读取判断实例状态。

开源启动配置读、预览、保存和重启共享同一授权限制；连接或授权变化时返回连接 Tab并卸载编辑器。云库模型配置属于 AK/SK 管理面，不能复用 User Key或 ov.conf 文件编辑器。

## 接口边界

- 火山使用 Authorization: Bearer；开源使用 X-API-Key。Provider 识别与角色判断分开，Key不跨授权面回退。[云端鉴权](https://docs.volcengine.com/docs/vector_database_vikingdb/APIAuthenticationInstructions?lang=zh)
- 云库模型信息公开接口为 GetOpenVikingCollection，需要 AK/SK和 ResourceID，并非 ov.conf 的文件版本、预览及重启协议。[库详情](https://docs.volcengine.com/docs/84313/2685089)
- Working Memory 保留版本差异：开源预设默认关闭，云端创建会话文档仍写默认开启。当前提交调用不透传新 enable_working_memory 参数。[创建会话](https://docs.volcengine.com/docs/84313/2487445)、[提交会话](https://docs.volcengine.com/docs/84313/2487441)
- 上述云端判断来自公开契约；此前 admin Key成功样本不能证明普通 User隔离或所有实例能力。[原接口审计](./api-contract-audit.md)

## 验证范围

回归开源原有页面、云端文件树和只读预览、个人／共享目录 Tab、详情跳转、旧链接、Key及连接切换、Gateway深链接、启动配置挂载和请求限制。浏览器使用模拟服务验证请求头与请求范围；生产实例写入、普通 User权限隔离和扩展契约仍需单独联调。
