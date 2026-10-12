# OpenViking TypeScript SDK

`@openviking/sdk` 是面向 Node.js 的轻量级 JavaScript/TypeScript HTTP SDK，没有运行时依赖。本 README 是 TypeScript SDK 唯一的中文使用文档，包含接入、异步任务、常用能力和类型入口。

## SDK 定位与支持范围

SDK 用于 Node.js 应用通过 HTTP 调用已有 OpenViking Server，覆盖资源与技能、文件系统和内容、检索、Session/Memory、Tags、任务、Watch、OVPack、ACL、状态观察、资产和租户管理能力。

它不包含 Server、本地运行时或 CLI。服务端 HTTP 契约和字段级解释仍以仓库 API 文档为准；本 README 描述 Node/TypeScript 调用方式和特有语义。

## 安装

```bash
npm install @openviking/sdk
```

要求 Node.js 18+ 和可访问的 OpenViking 服务，例如 `http://127.0.0.1:1933`。SDK 同时发布 ESM、CommonJS 和 TypeScript 声明；项目使用的模块加载方式由自身构建配置决定。

## 初始化、认证与配置

```ts
import { OpenVikingClient } from "@openviking/sdk";

const client = new OpenVikingClient({
  baseUrl: "http://127.0.0.1:1933",
  apiKey: "your-user-key",
  timeout: 120_000,
});

console.log(await client.health());
```

| `ClientConfig` 字段 | 请求头/行为 | 使用时机 |
|---|---|---|
| `apiKey` | `X-API-Key` | 普通 API key 部署的认证信息 |
| `account` | `X-OpenViking-Account` | trusted 部署或网关透传租户身份时使用 |
| `user` | `X-OpenViking-User` | 同上，指定用户身份 |
| `actorPeerId` | `X-OpenViking-Actor-Peer` | 请求关联特定 actor peer 时使用 |
| `timeout` | 请求超时（毫秒） | 为所有请求设置默认上限 |
| `uploadMode` | 临时上传模式 | 共享临时存储部署可用 `"shared"`，默认 `"local"` |

`headers` 可传应用自己的请求头，`fetch` 允许注入自定义实现。不要依赖旧 `agent_id` 入口；新代码使用 `actorPeerId`。

## 五分钟跑通：导入资源、等待任务并检索

`addResource` 对存在的 Node 本地文件自动上传，对目录先压缩再上传；其他字符串会作为 URL 或服务端路径提交。资源导入通常异步，必须查询本次返回的 `task_id` 到 `completed` 后再检索。

```ts
import { OpenVikingClient } from "@openviking/sdk";

const client = new OpenVikingClient({
  baseUrl: "http://127.0.0.1:1933",
  apiKey: "your-user-key",
});

const created = await client.addResource("./docs/guide.md", {
  to: "viking://resources/demo-guide",
  reason: "导入产品文档",
});
const taskId = String(created.task_id);

for (;;) {
  const task = await client.getTask(taskId);
  const status = task?.status;
  if (status === "completed") break;
  if (status === "failed" || status === "cancelled") {
    throw new Error(`resource task ${taskId} ended with ${String(status)}`);
  }
  await new Promise((resolve) => setTimeout(resolve, 500));
}

const results = await client.find("产品文档", {
  targetUri: "viking://resources/demo-guide",
  limit: 5,
});
console.log(results);
```

如果只需写入资源树和向量，不生成或刷新 `.abstract.md` / `.overview.md`，设置 `processingMode: "vectors_only"`。使用完整语义处理时保留服务端默认或选择相应处理模式。

## 核心能力

### 资源、技能与 Watch

`addResource`、`addSkill` 可返回后台任务。技能管理包括 `listSkills`、`findSkills`、`validateSkill`、`getSkill`、`updateSkill` 与 `deleteSkill`。资源导入设置 `watchInterval` 后可创建 Watch；使用 `listWatches`、`getWatch`、`updateWatch`、`triggerWatch` 与 `deleteWatch` 管理它。

```ts
const skillTask = await client.addSkill("./skills/search-web");
const watches = await client.listWatches({ activeOnly: true });
console.log(skillTask, watches);
```

### 文件系统、内容、Tags 与 ACL

使用 `list`/`listPage`、`tree`/`treePage`、`stat`、`attrs`、`mkdir`、`read`、`downloadBytes`、`write`、`batchWrite`、`move`、`remove`、`abstract`、`overview` 与 `reindex` 管理资源树。需要分页状态时使用 `listPage` 与 `treePage`，而不是只消费数组结果。

```ts
const page = await client.listPage("viking://resources", { limit: 50 });
await client.write("viking://resources/note.md", "# 笔记", {
  tags: ["team=search", "source=typescript"],
  tagMode: "replace",
});
console.log(page.hasMore);
```

`setTags` 的有效模式为 `replace`、`append`、`clear`；Tags 是调用方提供的业务元数据。ACL 使用 `aclGet`、`aclSet`、`aclGrant`、`aclRevoke`、`aclDelete`。

### 检索

`find` 用于无 Session 上下文的检索，`search` 带可选 Session 上下文，`searchContext` 生成可直接注入 Agent 的上下文；`grep` 与 `glob` 处理文本和路径匹配。

本地图片路径、`viking://`、`http(s)://`、data URI 可作为 `find`/`search` 的 `image`。本地图片会被转为 data URI，服务端须配置多模态 embedding 模型。

```ts
const posters = await client.search("红色海报风格", {
  targetUri: "viking://resources/images",
  image: "./poster.png",
  limit: 5,
});
```

已定义字段用 options 的 camelCase 名称传递。只有服务端先于 SDK 增加且当前版本尚未建模的字段，才可使用 `extra`；当 `extra` 与本次已序列化的请求字段冲突时会抛出错误。不要用它覆盖或绕过 SDK 已定义字段。

### Session、Memory 与任务

使用 `createSession`、`listSessions`、`getSession`、`updateSessionConfig`、`sessionExists`、`deleteSession`、`addMessage`、`batchAddMessages`、`getSessionContext`、`getSessionArchive`、`commitSession` 管理会话和记忆。

```ts
await client.createSession({
  sessionId: "demo-session",
  memoryExtractionConfig: { events: { tags: ["team=search"] } },
});
await client.addMessage("demo-session", {
  role: "user",
  content: "请记住我偏好简洁回答",
});
await client.commitSession("demo-session", {
  eventTags: ["team=search"],
});
```

大多数 options 中 `undefined`/`null` 都不会发送；但 `createSession` 和 `updateSessionConfig` 特别保留 `autoCommitPolicy: null`，用于显式禁用自动 commit。`eventTags: []` 表示本次 commit 显式跳过 Session 默认 Tags。

`getTask`、`listTasks`、`cancelTask` 管理具体任务。不要用全局 `waitProcessed()` 替代按本次 `task_id` 的轮询。

### OVPack、状态、资产与管理能力

- OVPack：`exportOVPack`、`backupOVPack` 将响应流写到 Node 本地路径；`importOVPack`、`restoreOVPack` 导入数据包。
- 状态：`health`、`isHealthy`、`getStatus`、`queueStatus`、`vikingDBStatus`、`modelsStatus`、`checkConsistency`。
- 资产：`resolveOpenVikingAssets`、`preflightOpenVikingAsset`。
- 管理：root key 可使用账户、用户、组、Account Settings、Agent Evolution 与 Experience 的 `admin...`、`listExperienceTrajectories`、`getExperienceOutcomes` 方法。

## 错误处理、超时、取消与易错点

服务端和 transport 错误以 `OpenVikingError` 抛出，使用 `isOpenVikingError` 判断并读取 `code`。单个请求可用 `AbortSignal` 取消；`timeout` 是客户端默认超时。

```ts
import { isOpenVikingError } from "@openviking/sdk";

const controller = new AbortController();
try {
  const healthy = await client.health({ signal: controller.signal });
  console.log(healthy);
} catch (error) {
  if (isOpenVikingError(error)) console.error(error.code, error.message);
  else throw error;
}
```

- 提交后台任务成功不代表处理完成，始终检查任务最终状态。
- 大多数 options 中 `undefined` 与 `null` 都不会出现在请求体；`createSession` 和 `updateSessionConfig` 的 `autoCommitPolicy: null` 是显式例外。需要清空其他状态时，使用该方法定义的显式模式或值，例如 `tagMode: "clear"`。
- `wait: false` 会被正确保留在请求中；不要因 JavaScript 假值而误以为它会自动省略。
- `reindex` 默认 `wait: true`、`recursive: true`；改为异步或非递归时必须显式传 `false`。

## API 速查表

| 场景 | 主要方法 |
|---|---|
| 资源、技能、Watch | `addResource`, `addSkill`, `listSkills`, `findSkills`, `validateSkill`, `getSkill`, `updateSkill`, `deleteSkill`, `listWatches`, `getWatch`, `updateWatch`, `triggerWatch`, `deleteWatch` |
| 文件与权限 | `list`, `listPage`, `tree`, `treePage`, `stat`, `attrs`, `mkdir`, `remove`, `move`, `read`, `downloadBytes`, `abstract`, `overview`, `write`, `batchWrite`, `setTags`, `reindex`, `aclGet`, `aclSet`, `aclGrant`, `aclRevoke`, `aclDelete` |
| 检索 | `find`, `search`, `searchContext`, `grep`, `glob` |
| Session 与任务 | `createSession`, `listSessions`, `getSession`, `updateSessionConfig`, `sessionExists`, `deleteSession`, `addMessage`, `batchAddMessages`, `getSessionContext`, `getSessionArchive`, `commitSession`, `getTask`, `listTasks`, `cancelTask`, `waitProcessed` |
| 数据包与系统 | `exportOVPack`, `backupOVPack`, `importOVPack`, `restoreOVPack`, `health`, `isHealthy`, `checkConsistency` 和 observer 状态方法 |
| 编译与快照 | `compile`, `gitCommit`, `gitRestore`, `gitShow`, `gitLog`, `gitDiff`, `gitGetIgnore`, `gitSetIgnore`, `gitDeleteIgnore` |
| 管理与演进 | 账户/用户/组管理 `admin...` 方法、Account Settings、Agent Evolution、Experience 与资产方法 |

精确 options、返回类型和联合类型以包导出的 TypeScript declaration 为准；IDE 自动补全是调用时的首选参考。

## 兼容性、升级与测试

SDK 支持 Node.js 18+，并与 OpenViking Server 分别发布。升级时阅读 release notes，重点检查后台任务、`undefined`/`null` 序列化、处理模式与目标服务端版本的兼容性。

本地开发验证：

```bash
cd sdk/typescript
npm install
npm run typecheck
npm test
```

发布使用 `typescript-sdk@<version>` 格式的 tag，例如 `typescript-sdk@0.1.0`；仓库 workflow 会构建并发布对应 npm 包。
