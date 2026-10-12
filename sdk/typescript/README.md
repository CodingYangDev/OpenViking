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

以下是当前 `OpenVikingClient` 的公开方法契约。URI 参数会规范化为
`viking://` 形式；`JsonObject` 表示服务端 `result` 的原样映射，SDK **不承诺**
其中的键或嵌套结构。除另有说明外，写入 JSON body 的可选字段在值为
`undefined` 或 `null` 时都会被移除，不会发到服务端；这不适用于 URL query
参数，也不应把它理解为服务端字段的清空操作。

### 方法契约

#### 资源、技能与 Watch

| 方法 | 参数 | 含义 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|---|
| `addResource` | `source: string`, `options: AddResourceOptions = {}` | `source` 是待导入的本地文件、目录、远程 URL 或服务端路径；`options` 指定落点、处理方式、异步等待与元数据。 | 存在的 Node 本地文件/目录先上传（目录会打包）；否则按路径提交。`to` 与 `parent` 互斥；提供 `tags` 时 `tagMode` 默认 `replace`，`tagMode: "clear"` 即使无 tags 也发送。 | `Promise<JsonObject>` |
| `addSkill` | `source: unknown`, `options: WaitOptions & { targetUri?, extra? } = {}` | `source` 是要安装的技能内容或本地路径；`targetUri` 限定安装目标；`wait/timeout` 控制任务等待。 | 仅存在的 Node 文件/目录上传；inline 内容和不存在路径字符串作为 `data`。`wait=false`。 | `Promise<JsonObject>` |
| `listSkills` | `options: { nodeLimit?, targetUri? } = {}` | `nodeLimit` 限制遍历技能节点数；`targetUri` 限定检索根。 | `nodeLimit=1000`。 | `Promise<JsonObject>` |
| `findSkills` | `query: string`, `options = {}` | `query` 是语义检索词；options 的 `limit/scoreThreshold/level/targetUri/telemetry` 分别控制数量、最低分、层级、范围和观测数据。 | `limit=10`。 | `Promise<JsonObject>` |
| `validateSkill` | `data: unknown`, `options = {}` | `data` 是待校验、但不安装的技能内容；`strict` 切换严格检查；`sourcePath/skillDirName/targetUri` 提供来源和目标上下文。 | `strict=false`。 | `Promise<JsonObject>` |
| `getSkill` | `name: string`, `options: GetSkillOptions = {}` | `name` 是技能名；options 决定是否带内容、文件、来源、层级及目标根。 | 未给 `includeContent` 则省略；`includeFiles=true`、`includeSource=false`，显式 `false` 保留。 | `Promise<JsonObject>` |
| `updateSkill` | `name: string`, `source: unknown`, `options = {}` | `name` 标识要替换的技能；`source` 是新内容/路径；`sourceMetadata` 是来源元数据，其他 options 与安装时相同。 | 本地路径与 inline 的判定同 `addSkill`；`wait=false`。 | `Promise<JsonObject>` |
| `deleteSkill` | `name: string`, `targetUri?: string` | `name` 是删除对象；`targetUri` 用于同名技能的目标范围。 | 未给 `targetUri` 不带 query。 | `Promise<JsonObject>` |
| `listWatches` | `options: { activeOnly?, toUri? } = {}` | `activeOnly` 只返回启用的 Watch；`toUri` 按资源目标筛选。 | `activeOnly=false`；给 `toUri` 时规范化 URI。 | `Promise<JsonObject>` |
| `getWatch` | `taskId: string`, `toUri?: string` | `taskId` 是 Watch 任务 ID；`toUri` 是同一目标的辅助限定。 | `toUri` 给出时规范化。 | `Promise<JsonObject>` |
| `updateWatch` | `ref: { taskId?, toUri? }`, `changes: UpdateWatchOptions` | `ref` 用任务 ID 或目标 URI 定位 Watch；`changes` 是要修改的调度/启用状态/导入说明。 | 两个引用至少给一个；有 `taskId` 时优先。 | `Promise<JsonObject>` |
| `deleteWatch` | `ref: { taskId?, toUri? }` | `ref` 用任务 ID 或目标 URI 定位要删除的 Watch。 | 至少一个引用；`taskId` 优先。 | `Promise<JsonObject>` |
| `triggerWatch` | `ref: { taskId?, toUri? }` | `ref` 用任务 ID 或目标 URI 定位并立即触发的 Watch。 | 至少一个引用；`taskId` 优先。 | `Promise<JsonObject>` |

#### 检索、目录与内容

| 方法 | 参数 | 含义 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|---|
| `find` | `query: string`, `options: FindOptions = {}` | `query` 是无会话语义检索词；options 负责目标范围、过滤、时间、Tags、图像和结果预算。 | 无 Session；本地 `image` 转 data URI。 | `Promise<FindResult>` |
| `search` | `query: string`, `options: SearchOptions = {}` | `query` 是检索词；在 `FindOptions` 外，`sessionId` 提供会话上下文，`searchType` 选择 semantic/keywords。 | 本地 `image` 转 data URI。 | `Promise<FindResult>` |
| `searchContext` | `query: string`, `options: SearchContextOptions = {}` | `query` 是为 Agent 组装上下文的检索词；options 还控制 token 配额、去重、改写和 peer 范围。 | 固定发送 `mode="context"`；本地 `image` 转 data URI。 | `Promise<SearchContextResult>` |
| `grep` | `uri: string`, `pattern: string`, `options: GrepOptions = {}` | `uri` 是搜索根；`pattern` 是文本模式；options 控制大小写、扫描上限、层级、排除 URI 和 Tags。 | `caseInsensitive=false`、`nodeLimit=256`；`includeTags` 仅 `true` 发送。 | `Promise<JsonObject>` |
| `glob` | `pattern: string`, `uri = "viking://"`, `options: GlobOptions \| number = {}` | `pattern` 是路径 glob；`uri` 是起点；options 是节点上限和 Tags，数字第三参兼容为 `nodeLimit`。 | `uri="viking://"`、`nodeLimit=256`。 | `Promise<JsonObject>` |
| `list` | `uri: string`, `options: ListOptions = {}` | `uri` 是列目录；options 控制递归、输出内容、分页、排序和 Tags。 | 仅返回 `listPage().result`，不含分页状态。 | `Promise<unknown[]>` |
| `listPage` | `uri: string`, `options: ListOptions = {}` | 参数同 `list`，但保留 `hasMore` 以供调用方翻页。 | `simple/recursive/showAllHidden=false`、`output="original"`、`absLimit=256`、`overviewLimit=4000`、`nodeLimit=1000`。 | `Promise<ListPage>` |
| `tree` | `uri: string`, `options: TreeOptions = {}` | `uri` 是树根；options 控制树深、目录筛选、分页、输出内容和 Tags。 | 仅返回 `treePage().result`。 | `Promise<JsonObject[]>` |
| `treePage` | `uri: string`, `options: TreeOptions = {}` | 参数同 `tree`，但保留 `hasMore`。 | `output="original"`、`absLimit=128`、`overviewLimit=4000`、`showAllHidden=false`、`nodeLimit=1000`、`levelLimit=3`；`directoriesOnly/includeTags` 仅 `true` 发送。 | `Promise<TreePage>` |
| `stat` | `uri: string` | `uri` 是读取元数据的资源路径。 | URI 规范化。 | `Promise<JsonObject>` |
| `attrs` | `uri: string` | `uri` 是读取逻辑属性（包括可检索元数据）的路径。 | URI 规范化。 | `Promise<JsonObject>` |
| `mkdir` | `uri: string`, `description?: string`, `acl?: AclSpec` | `uri` 是新目录；`description` 是目录说明；`acl` 是访问规则。 | 可选 body 字段按 compact 删除。 | `Promise<void>` |
| `read` | `uri: string`, `offset = 0`, `limit = -1` | `uri` 是文本文件；`offset` 为起始位置；`limit` 为读取上限。 | `-1` 原样发送，表示服务端默认的非限量读取语义。 | `Promise<string>` |
| `downloadBytes` | `uri: string` | `uri` 是要下载原始二进制内容的资源。 | JSON 错误仍解析并抛出。 | `Promise<Uint8Array>` |
| `abstract` | `uri: string` | `uri` 是读取其 L0 摘要的资源。 | URI 规范化。 | `Promise<string>` |
| `overview` | `uri: string` | `uri` 是读取其 L1 概览的资源。 | URI 规范化。 | `Promise<string>` |
| `write` | `uri: string`, `content: string`, `options: WriteOptions = {}` | `uri` 是目标文件；`content` 是文本（空串可清空）；options 指定写入模式、后处理、Tags、ACL 与等待。 | 提供 tags 时 `tagMode=replace`；`clear` 强制发送。 | `Promise<JsonObject>` |
| `batchWrite` | `rootUri: string`, `operations: BatchWriteOperation[]`, `options: BatchWriteOptions = {}` | `rootUri` 是批写锁定/刷新根；每个 operation 指定相对/绝对目标和内容；options 控制任务等待。 | 每个 operation URI 规范化；`content/contentBase64` 的 null/undefined 删除。 | `Promise<JsonObject>` |
| `setTags` | `uri: string`, `tags?: string[]`, `options: SetTagsOptions = {}` | `uri` 是打标目标；`tags` 是业务检索标签；options 控制替换/追加/清空、递归和 telemetry。 | `mode=replace`、`recursive=false`；`extra` 不可覆盖内建字段。 | `Promise<JsonObject>` |
| `reindex` | `uri: string`, `options: ReindexOptions = {}` | `uri` 是重建范围；options 控制重建模式、同步等待、强制、递归及重建后 Tags。 | `mode="vectors_only"`、`wait=true`、`recursive=true`；`force` 只在 true 发送。 | `Promise<JsonObject>` |
| `remove` | `uri: string`, `options = {}` | `uri` 是删除目标；`recursive` 允许目录递归，`wait/timeout` 控制服务端等待。 | `recursive=false`、`wait=false`；未给 `timeout` 不带 query。 | `Promise<void>` |
| `move` | `fromUri: string`, `toUri: string` | `fromUri` 是源路径，`toUri` 是目标路径。 | 两端 URI 规范化。 | `Promise<void>` |

#### ACL、Session、消息与任务

| 方法 | 参数 | 含义 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|---|
| `aclGet` | `uri: string` | `uri` 是读取 ACL 的资源或目录。 | URI 规范化。 | `Promise<JsonObject>` |
| `aclSet` | `uri: string`, `acl: AclSpec` | `uri` 是设置目标；`acl` 给出继承模式和完整授权条目。 | `uri` 与 acl 顶层字段合并发送。 | `Promise<JsonObject>` |
| `aclGrant` | `uri: string`, `principal: string`, `level` | `uri` 是授权范围；`principal` 是被授权主体；`level` 是 read/write/manage 权限级别。 | URI 规范化。 | `Promise<JsonObject>` |
| `aclRevoke` | `uri: string`, `principal: string` | `uri` 是撤权范围；`principal` 是被撤销的主体。 | URI 规范化。 | `Promise<JsonObject>` |
| `aclDelete` | `uri: string` | `uri` 是删除其显式 ACL 的资源或目录。 | URI 规范化。 | `Promise<JsonObject>` |
| `createSession` | `options: CreateSessionOptions = {}` | options 指定客户端会话 ID、记忆策略、自动 commit 策略、提取配置及观测数据。 | **存在性例外：**只有对象拥有 `autoCommitPolicy` 时才发送；值为 `undefined` 或 `null` 都发送 JSON `null`，完全省略才继承服务端默认。 | `Promise<JsonObject>` |
| `listSessions` | 无 | 返回当前身份可见的会话。 | — | `Promise<unknown[]>` |
| `getSession` | `sessionId: string`, `autoCreate = false` | `sessionId` 是目标会话；`autoCreate` 控制不存在时是否由服务端新建。 | 仅 true 发送 `auto_create=true`。 | `Promise<JsonObject>` |
| `updateSessionConfig` | `sessionId: string`, `options: UpdateSessionConfigOptions` | `sessionId` 是更新目标；options 修改提取配置或自动 commit 策略。 | `autoCommitPolicy` 保持与 `createSession` 相同的存在性三态。 | `Promise<JsonObject>` |
| `sessionExists` | `sessionId: string` | `sessionId` 是待探测会话。 | `NOT_FOUND`/HTTP 404 返回 `false`，其余错误抛出。 | `Promise<boolean>` |
| `deleteSession` | `sessionId: string` | `sessionId` 是要删除的会话。 | — | `Promise<void>` |
| `getSessionContext` | `sessionId: string`, `tokenBudget = 128_000` | `sessionId` 是会话；`tokenBudget` 是服务端组装上下文可使用的 token 上限。 | `tokenBudget=128000`，作为 `token_budget` query。 | `Promise<JsonObject>` |
| `getSessionArchive` | `sessionId: string`, `archiveId: string` | `sessionId` 是会话；`archiveId` 是其已提交归档版本。 | — | `Promise<JsonObject>` |
| `addMessage` | `sessionId: string`, `message: Message` | `sessionId` 是追加目标；`message` 是单条对话或工具事件。 | 必须有 content 或非空 parts；非空 parts 优先，content 不发送。 | `Promise<JsonObject>` |
| `batchAddMessages` | `sessionId: string`, `messages: Message[]`, `telemetry?: unknown` **或** `options?: BatchAddMessagesOptions` | `sessionId` 是追加目标；`messages` 是按序批量消息；第三参兼容旧 telemetry 或新调用级 options。 | 每条按 `addMessage` 验证；不含 `telemetry/extra` 的对象按旧 telemetry 处理。 | `Promise<JsonObject>` |
| `commitSession` | `sessionId: string`, `options?: CommitSessionOptions` **或** `(keepRecentCount?, telemetry?, eventTags?)` | `sessionId` 是待提交会话；options 控制记忆提取、保留窗口、工作记忆和本次事件 Tags；旧位置参数分别是保留条数、观测数据、Tags。 | 旧位置参数兼容；`enableWorkingMemory:null` 被 compact 删除、继承会话策略；`eventTags: []` 显式发送空 tags。 | `Promise<JsonObject>` |
| `getTask` | `taskId: string` | `taskId` 是某次导入、编译等后台任务的 ID。 | `NOT_FOUND`/HTTP 404 返回 `null`；任务对象不承诺稳定字段。 | `Promise<JsonObject \| null>` |
| `cancelTask` | `taskId: string` | `taskId` 是要请求取消的后台任务。 | — | `Promise<JsonObject>` |
| `listTasks` | `options: TaskListOptions = {}` | options 的 `taskType/status/resourceId/limit` 分别筛选任务种类、状态、资源和数量。 | 未给筛选项则省略对应 query。 | `Promise<unknown[]>` |
| `waitProcessed` | `timeout?: number` | `timeout` 是等待全局队列处理完成的上限，不关联某一个任务。 | 未给时 body 无 `timeout`；不能代替轮询本次 `taskId`。 | `Promise<JsonObject>` |

#### OVPack、编译、状态与快照

| 方法 | 参数 | 含义 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|---|
| `exportOVPack` | `uri: string`, `to: string`, `includeVectors = false`, `options: RequestOptions = {}` | `uri` 是导出资源树；`to` 是 Node 本地输出文件/目录；`includeVectors` 决定是否打入向量；`signal` 可取消下载。 | 响应流写入本地，返回实际文件路径。 | `Promise<string>` |
| `backupOVPack` | `to: string`, `includeVectors = false`, `options: RequestOptions = {}` | `to` 是 Node 本地 restore-only 备份路径；`includeVectors` 决定是否含向量；options 提供取消信号。 | 返回实际备份路径。 | `Promise<string>` |
| `importOVPack` | `source: string`, `parent: string`, `options: ImportPackOptions = {}` | `source` 是已有 Node 本地 OVPack 文件；`parent` 是导入挂载根；options 指定冲突和向量策略。 | 目录/远程路径抛 `TypeError`；返回服务端 uri。 | `Promise<string>` |
| `restoreOVPack` | `source: string`, `options: ImportPackOptions = {}` | `source` 是已有 Node 本地备份文件；options 指定冲突和向量策略。 | 目录/远程路径抛 `TypeError`；返回服务端 uri。 | `Promise<string>` |
| `compile` | `fromUris: string[]`, `to: string`, `skill: string`, `options: CompileOptions = {}` | `fromUris` 是待编译资源；`to` 是产物位置；`skill` 是编译技能；options 提供说明和参数。 | 空 `args` 不发送；`extra` 不可覆盖 `instruction/args`。 | `Promise<JsonObject>` |
| `health` | `options: RequestOptions = {}` | `signal` 用于取消原始健康检查。 | 仅 HTTP 成功且 JSON `status="ok"` 返回 true。 | `Promise<boolean>` |
| `checkConsistency` | `uri: string` | `uri` 是要检测文件系统/索引一致性的范围。 | URI 规范化。 | `Promise<JsonObject>` |
| `getStatus` | `format?: "table" \| "json"` | `format` 选择聚合状态的展示结构。 | 未给不带 query。 | `Promise<JsonObject>` |
| `queueStatus` | `format?: "table" \| "json"` | `format` 选择队列观察数据的展示结构。 | 未给不带 query。 | `Promise<JsonObject>` |
| `vikingDBStatus` | `format?: "table" \| "json"` | `format` 选择 VikingDB 观察数据的展示结构。 | 未给不带 query。 | `Promise<JsonObject>` |
| `modelsStatus` | `format?: "table" \| "json"` | `format` 选择模型观察数据的展示结构。 | 未给不带 query。 | `Promise<JsonObject>` |
| `isHealthy` | 无 | 用聚合 observer 状态提供轻量健康判断。 | 仅 `getStatus().is_healthy === true` 返回 true。 | `Promise<boolean>` |
| `gitCommit` | `options: GitCommitOptions` | options 指定快照说明、包含路径、分支和作者署名。 | `message` 必填；`branch="main"`。 | `Promise<JsonObject>` |
| `gitRestore` | `options: GitRestoreOptions` | options 指定源 commit、恢复目录、目标分支、试运行和恢复提交署名。 | `sourceCommit` 必填；`branch="main"`、`dryRun=false`。 | `Promise<JsonObject>` |
| `gitShow` | `targetRef: string`, `path?: string` | `targetRef` 是快照引用；`path` 可选，指定要读取的快照内文件。 | 二进制响应构造 `GitBlob`，否则返回原样 JSON。 | `Promise<JsonObject \| GitBlob>` |
| `gitLog` | `branch = "main"`, `limit = 20`, `paths?: string[]` | `branch` 是查看分支；`limit` 是最多返回条数；`paths` 限定相关路径。 | 按新到旧返回。 | `Promise<JsonObject[]>` |
| `gitDiff` | `path: string`, `toRef: string`, `fromRef?: string` | `path` 是比较文件；`toRef` 是目标版本；`fromRef` 是可选基线版本。 | `path` 规范化。 | `Promise<JsonObject>` |
| `gitGetIgnore` | 无 | 读取当前账户的 `.ovgitignore`。 | 非字符串 result 归一为 `""`。 | `Promise<string>` |
| `gitSetIgnore` | `content: string` | `content` 是完整覆盖写入的 `.ovgitignore` 文本。 | — | `Promise<void>` |
| `gitDeleteIgnore` | 无 | 删除当前账户的 `.ovgitignore`。 | — | `Promise<void>` |

#### 管理、Experience 与 Assets

| 方法 | 参数 | 含义 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|---|
| `adminCreateAccount` | `accountId: string`, `adminUserId: string`, `options = {}` | `accountId` 是新租户 ID；`adminUserId` 是其首位管理员；`userConfig/seed` 分别是用户配置和可复现密钥种子。 | 可选字段未给即省略。 | `Promise<JsonObject>` |
| `adminListAccounts` | `options = { name?, limit?, page? }` | `name` 是支持 `*`/`?` 的账户名模式；`limit/page` 是分页大小/页号。 | 只有给 `limit` 才发送 page，page 默认 `1`。 | `Promise<unknown[]>` |
| `adminDeleteAccount` | `accountId: string` | `accountId` 是要删除的租户。 | — | `Promise<JsonObject>` |
| `adminRegisterUser` | `accountId: string`, `userId: string`, `role: string`, `options = {}` | 前三项依次为所属租户、用户 ID、角色；`userConfig/seed` 与建账户时相同。 | 可选字段未给即省略。 | `Promise<JsonObject>` |
| `adminListUsers` | `accountId: string`, `options = { limit?, name?, role?, page? }` | `accountId` 限定租户；`name` 是通配模式，`role` 是角色筛选，`limit/page` 是分页。 | page 规则同 `adminListAccounts`。 | `Promise<unknown[]>` |
| `adminRemoveUser` | `accountId: string`, `userId: string` | 分别为所属租户和要移除的用户。 | — | `Promise<JsonObject>` |
| `adminSetRole` | `accountId: string`, `userId: string`, `role: string` | 分别为所属租户、目标用户和要授予的角色。 | — | `Promise<JsonObject>` |
| `adminRegenerateKey` | `accountId: string`, `userId: string`, `seed?: string` | 前两项定位用户；`seed` 可让生成结果可复现。 | seed 为 undefined 时请求无 body。 | `Promise<JsonObject>` |
| `adminMigrate` | `cleanup = false` | `cleanup` 选择执行旧数据迁移，还是清理迁移数据。 | false 发送 `migrate`，true 发送 `cleanup`。 | `Promise<JsonObject>` |
| `adminGetAgentEvolution` | 无 | 读取当前调用方账户有效的 Agent Evolution 开关。 | — | `Promise<JsonObject>` |
| `adminSetAgentEvolution` | `enabled: boolean` | `enabled` 开关当前账户的 Agent Evolution。 | 持久化并热加载。 | `Promise<JsonObject>` |
| `adminGetAccountSettings` | `accountId: string` | `accountId` 是读取有效/显式覆盖设置的租户。 | 返回原样映射。 | `Promise<JsonObject>` |
| `adminSetAccountAgentEvolution` | `accountId: string`, `enabled: boolean` | `accountId` 是 allowlist 设置目标；`enabled` 是该账户的开关值。 | — | `Promise<JsonObject>` |
| `listExperienceTrajectories` | `experienceUri: string`, `options: ExperienceTrajectoryOptions = {}` | `experienceUri` 是 Experience 资源；options 的 limit/offset/date 区间筛选消费它的轨迹。 | URI 规范化；未给项不发 query。 | `Promise<JsonObject>` |
| `getExperienceOutcomes` | `experienceUri: string`, `options: ExperienceOutcomeOptions = {}` | `experienceUri` 是 Experience 资源；options 给聚合结果的日期区间。 | URI 规范化；未给项不发 query。 | `Promise<JsonObject>` |
| `resolveOpenVikingAssets` | `manifestYaml: string`, `options: ResolveAssetsOptions = {}` | `manifestYaml` 是要解析的 Assets 清单；options 可补充 catalog 和两个诊断标签。 | `extra` 不可覆盖已发送字段。 | `Promise<JsonObject>` |
| `preflightOpenVikingAsset` | `name: string`, `repoUrl: string`, `options: PreflightAssetOptions = {}` | `name` 是资产名；`repoUrl` 是 Git 仓库；options 可钉住分支/commit并提供一次性凭据。 | 固定 `connector="git"`。 | `Promise<JsonObject>` |

### 字段契约

下表是字段级契约的权威阅读入口：一行只解释一个字段。未特别说明的
`undefined`/`null` 选项会被 `compact` 省略；`false`、`0`、空字符串和空数组会保留。
`extra` 仅用于服务端新增、SDK 尚未建模的字段，且不能覆盖方法已保护的字段。

#### 配置、导入、写入与 ACL 字段明细

| 类型 | 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|---|
| `ClientConfig` | `baseUrl` | `string` | 必填 | OpenViking Server 根地址。 |
| `ClientConfig` | `apiKey` | `string` | 省略则不发 `X-API-Key` | 普通 API key 认证凭据。 |
| `ClientConfig` | `account` | `string` | 可省略 | trusted/网关部署的账户身份。 |
| `ClientConfig` | `user` | `string` | 可省略 | trusted/网关部署的用户身份。 |
| `ClientConfig` | `actorPeerId` | `string` | 可省略 | 请求关联的 actor peer。 |
| `ClientConfig` | `timeout` | `number` | 可省略 | 客户端请求超时毫秒数。 |
| `ClientConfig` | `headers` | `ClientHeaders` | 可省略 | 追加的应用请求头。 |
| `ClientConfig` | `fetch` | `typeof fetch` | 可省略 | 注入 transport 的 fetch 实现。 |
| `ClientConfig` | `profile` | `boolean` | 可省略 | 请求 profiling 开关。 |
| `ClientConfig` | `uploadMode` | `"local" \| "shared"` | `"local"` | 临时上传存储模式；共享存储用 `shared`。 |
| `WaitOptions` | `wait` | `boolean` | 由具体方法决定 | 是否等待后台任务完成；`false` 不会被 compact 丢弃。 |
| `WaitOptions` | `timeout` | `number` | 可省略 | 任务等待超时。 |
| `WaitOptions` | `telemetry` | `unknown` | 可省略 | 附加给服务端的调用观测信息。 |
| `AclSpec` | `acl_mode` | `"inherit" \| "restricted"` | 可省略 | 继承父级 ACL 或启用受限 ACL。 |
| `AclSpec` | `entries` | `{ principal, level }[]` | 可省略 | 主体到权限级别的完整授权表。 |
| `AclSpec.entries[]` | `principal` | `string` | 必填 | 被授权主体标识。 |
| `AclSpec.entries[]` | `level` | `"read" \| "write" \| "manage"` | 必填 | 对该主体授予的权限。 |
| `AddResourceOptions` | `acl` | `AclSpec` | 可省略 | 导入资源的 ACL。 |
| `AddResourceOptions` | `to` | `string` | 与 `parent` 互斥 | 导入后资源的精确目标 URI。 |
| `AddResourceOptions` | `parent` | `string` | 与 `to` 互斥 | 导入资源的父目录 URI。 |
| `AddResourceOptions` | `createParent` | `boolean` | 可省略 | 父目录不存在时是否创建。 |
| `AddResourceOptions` | `reason` | `string` | 可省略 | 导入目的，供处理流程参考。 |
| `AddResourceOptions` | `instruction` | `string` | 可省略 | 导入/语义处理的额外指令。 |
| `AddResourceOptions` | `strict` | `boolean` | 可省略 | 是否要求严格处理/校验。 |
| `AddResourceOptions` | `ignoreDirs` | `string` | 可省略 | 要忽略目录的模式或规则。 |
| `AddResourceOptions` | `include` | `string` | 可省略 | 允许导入文件的模式。 |
| `AddResourceOptions` | `exclude` | `string` | 可省略 | 排除文件的模式。 |
| `AddResourceOptions` | `directlyUploadMedia` | `boolean` | 可省略 | 是否直接上传媒体而非普通解析。 |
| `AddResourceOptions` | `preserveStructure` | `boolean` | 可省略 | 是否保持源目录层级。 |
| `AddResourceOptions` | `watchInterval` | `number` | 可省略 | 创建 Watch 时的轮询间隔。 |
| `AddResourceOptions` | `processingMode` | `"semantic_and_vectors" \| "vectors_only"` | 可省略 | 语义+向量处理，或仅生成向量。 |
| `AddResourceOptions` | `args` | `JsonObject` | 空对象省略 | 传给处理器的附加参数。 |
| `AddResourceOptions` | `tags` | `string[]` | 可省略 | 资源的业务检索标签。 |
| `AddResourceOptions` | `tagMode` | `"replace" \| "append" \| "clear"` | tags 给出时默认为 `replace` | 如何应用 tags；`clear` 可不带 tags。 |
| `AddResourceOptions` | `extra` | `JsonObject` | 可省略 | 未建模服务端扩展字段。 |
| `WriteOptions` | `acl` | `AclSpec` | 可省略 | 写入文件的 ACL。 |
| `WriteOptions` | `mode` | `string` | 可省略 | 服务端写入模式。 |
| `WriteOptions` | `processingMode` | `ProcessingMode` | 可省略 | 写入后的语义/向量处理策略。 |
| `WriteOptions` | `tags` | `string[]` | 可省略 | 写入文件的检索标签。 |
| `WriteOptions` | `tagMode` | `"replace" \| "append" \| "clear"` | tags 给出时为 `replace` | 标签合并或清空方式。 |
| `WriteOptions` | `extra` | `JsonObject` | 可省略 | 未建模扩展字段。 |
| `BatchWriteOperation` | `uri` | `string` | 必填 | 本批次内待写文件 URI。 |
| `BatchWriteOperation` | `content` | `string` | 可省略 | 文本内容；与二进制内容二选一由服务端解释。 |
| `BatchWriteOperation` | `contentBase64` | `string` | 可省略 | Base64 编码的二进制内容。 |
| `BatchWriteOperation` | `mode` | `"replace" \| "append" \| "create" \| "upsert"` | 可省略 | 覆盖、追加、仅新建或存在即更新。 |
| `BatchWriteOptions` | `extra` | `JsonObject` | 可省略 | 批写扩展字段；也继承 WaitOptions。 |
| `SetTagsOptions` | `mode` | `"replace" \| "append" \| "clear"` | `"replace"` | 标签替换、追加或显式清空。 |
| `SetTagsOptions` | `recursive` | `boolean` | `false` | 是否对目标子树递归打标。 |
| `SetTagsOptions` | `telemetry` | `unknown` | 可省略 | 调用观测信息。 |
| `SetTagsOptions` | `extra` | `JsonObject` | 可省略 | 未建模扩展字段。 |
| `ReindexOptions` | `mode` | `string` | `"vectors_only"` | 重建处理模式；不要与 write 的 `processingMode` 混用。 |
| `ReindexOptions` | `wait` | `boolean` | `true` | 是否同步等待重建任务。 |
| `ReindexOptions` | `force` | `boolean` | `false` 且仅 true 发送 | 是否强制重建。 |
| `ReindexOptions` | `recursive` | `boolean` | `true` | 是否遍历子树。 |
| `ReindexOptions` | `tags` | `string[]` | 可省略 | 重建记录要应用的 Tags。 |
| `ReindexOptions` | `tagMode` | `"replace" \| "append" \| "clear"` | tags 给出时为 `replace` | Tags 应用方式。 |
| `ReindexOptions` | `extra` | `JsonObject` | 可省略 | 不得覆盖 `force/tags/tag_mode` 的扩展字段。 |

#### 检索、目录、Session 与任务字段明细

| 类型 | 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|---|
| `FindOptions` | `targetUri` | `string \| string[]` | 可省略 | 一个或多个检索目标根。 |
| `FindOptions` | `image` | `string` | 可省略 | 图文检索图片；本地路径会转 data URI。 |
| `FindOptions` | `limit` | `number` | 可省略 | 返回命中数上限。 |
| `FindOptions` | `nodeLimit` | `number` | 可省略 | 扫描节点上限。 |
| `FindOptions` | `scoreThreshold` | `number` | 可省略 | 过滤低于此分数的命中。 |
| `FindOptions` | `filter` | `JsonObject` | 可省略 | 服务端结构化过滤条件。 |
| `FindOptions` | `contextType` | `unknown` | 可省略 | 限制检索上下文类别。 |
| `FindOptions` | `telemetry` | `unknown` | 可省略 | 调用观测信息。 |
| `FindOptions` | `since` / `until` | `string` | 可省略 | 时间范围下界 / 上界。 |
| `FindOptions` | `timeField` | `string` | 可省略 | 时间过滤使用的字段名。 |
| `FindOptions` | `level` | `number[]` | 可省略 | 限制检索的资源层级。 |
| `FindOptions` | `tags` | `string[]` | 可省略 | 必须匹配的检索 Tags。 |
| `FindOptions` | `includeProvenance` | `boolean` | 可省略 | 是否返回溯源信息。 |
| `FindOptions` | `readContent` | `boolean` | 可省略 | 是否读取命中原始内容。 |
| `FindOptions` | `eventsTimeDecayProtection` | `string \| null` | null 被省略 | 事件记忆时效衰减保护设置。 |
| `FindOptions` | `extra` | `JsonObject` | 可省略 | 未建模检索扩展字段。 |
| `SearchOptions` | `sessionId` | `string` | 可省略 | 为 search 提供会话上下文。 |
| `SearchOptions` | `searchType` | `"semantic" \| "keywords"` | 可省略 | 选择语义检索或关键词检索。 |
| `SearchContextOptions` | `searchType` / `image` / `sessionId` | 同 `SearchOptions` | 可省略 | 上下文组装的检索类型、图片和会话。 |
| `SearchContextOptions` | `limit` / `nodeLimit` / `scoreThreshold` | `number` | 可省略 | 结果数、扫描数和最低分预算。 |
| `SearchContextOptions` | `filter` / `contextType` / `tags` | `JsonObject` / `unknown` / `string[]` | 可省略 | 结构化、类别和标签筛选。 |
| `SearchContextOptions` | `since` / `until` / `timeField` | `string` | 可省略 | 时间范围和时间字段。 |
| `SearchContextOptions` | `eventsTimeDecayProtection` | `string \| null` | null 被省略 | 事件时效保护设置。 |
| `SearchContextOptions` | `queryExpansion` | `"off" \| "auto"` | 可省略 | 是否自动扩展查询。 |
| `SearchContextOptions` | `maxTokens` | `number` | 可省略 | 输出上下文 token 预算。 |
| `SearchContextOptions` | `quotas` | `Record<string, number>` | 可省略 | 各类别的结果配额。 |
| `SearchContextOptions` | `purpose` | `"chat" \| "coding"` | 可省略 | 上下文的使用场景。 |
| `SearchContextOptions` | `detail` | `string \| Record<string,string>` | 可省略 | 详情输出级别或按类设置。 |
| `SearchContextOptions` | `dedupTurns` | `number` | 可省略 | 去重时参考的最近轮数。 |
| `SearchContextOptions` | `excludeUris` | `string[]` | 可省略 | 排除的资源 URI。 |
| `SearchContextOptions` | `peerScope` | `"actor" \| "all"` | 可省略 | peer 检索范围。 |
| `SearchContextOptions` | `otherPeerPenalty` | `number \| Record<string,number>` | 可省略 | 非当前 peer 的分数惩罚。 |
| `SearchContextOptions` | `rewrite` / `rewriteMaxBullets` | `boolean \| "auto"` / `number` | 可省略 | 上下文改写开关 / 改写条目上限。 |
| `SearchContextOptions` | `telemetry` / `extra` | `unknown` / `JsonObject` | 可省略 | 观测数据 / 未建模扩展字段。 |
| `ListOptions` | `simple` / `recursive` | `boolean` | `false` | 简化条目 / 是否递归列目录。 |
| `ListOptions` | `output` | `string` | `"original"` | 条目输出形式。 |
| `ListOptions` | `absLimit` / `overviewLimit` | `number` | `256` / `4000` | 摘要和概览长度预算。 |
| `ListOptions` | `includeAbstract` / `includeOverview` | `boolean` | 可省略 | 是否附带摘要 / 概览。 |
| `ListOptions` | `showAllHidden` | `boolean` | `false` | 是否返回隐藏节点。 |
| `ListOptions` | `nodeLimit` / `offset` / `limit` | `number` | `1000` / 可省略 / 可省略 | 扫描上限、分页偏移和页大小。 |
| `ListOptions` | `sortBy` / `sortOrder` | `"name"\|"mtime"` / `"asc"\|"desc"` | 可省略 | 排序键和方向。 |
| `ListOptions` | `extraFields` / `tags` / `includeTags` | `("locked"\|"id"\|"count")[]` / `string[]` / `boolean` | includeTags 仅 true 发送 | 额外列、标签筛选及返回标签。 |
| `TreeOptions` | `output` / `absLimit` / `overviewLimit` | `string` / `number` / `number` | `"original"` / `128` / `4000` | 树输出形式及文本预算。 |
| `TreeOptions` | `includeAbstract` / `includeOverview` / `showAllHidden` | `boolean` | 可省略 / 可省略 / `false` | 是否附带摘要、概览和隐藏节点。 |
| `TreeOptions` | `directoriesOnly` | `boolean` | 仅 true 发送 | 是否只返回目录。 |
| `TreeOptions` | `nodeLimit` / `levelLimit` / `offset` / `limit` | `number` | `1000` / `3` / 可省略 / 可省略 | 扫描、深度和分页预算。 |
| `TreeOptions` | `extraFields` / `tags` / `includeTags` | 同 `ListOptions` | includeTags 仅 true 发送 | 额外列、标签筛选及返回标签。 |

#### Session、结果、快照、Assets 与错误字段明细

| 类型 | 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|---|
| `Message` | `role` | `string` | 必填 | 消息发送方角色。 |
| `Message` | `content` | `string` | 与非空 `parts` 至少一个 | 文本内容；非空 parts 时不发送。 |
| `Message` | `parts` | `JsonObject[]` | 可省略 | 多段结构化消息内容；非空时优先。 |
| `Message` | `createdAt` | `string` | 可省略 | 消息创建时间。 |
| `Message` | `peerId` | `string` | 可省略 | 消息归属 peer。 |
| `Message` | `turnId` | `string` | 可省略 | 对话轮 ID。 |
| `Message` | `messageKind` | `"user_query"\|"assistant_step"\|"tool_transport"\|"checkpoint"` | 可省略 | 消息在记忆/对话中的业务类别。 |
| `Message` | `sourceMessageIds` | `string[]` | 可省略 | 该消息引用的来源消息 ID。 |
| `Message` | `telemetry` | `unknown` | 单条 addMessage 可省略 | 单消息观测数据；batchAddMessages 不逐条发送。 |
| `CreateSessionOptions` | `sessionId` | `string` | 可省略 | 调用方指定的会话 ID。 |
| `CreateSessionOptions` | `memoryPolicy` | `JsonObject` | 可省略 | 会话记忆策略。 |
| `CreateSessionOptions` | `autoCommitPolicy` | `JsonObject \| null` | 属性不存在才省略 | 自动提交策略；属性存在时 `undefined/null` 都显式发 JSON null（禁用）。 |
| `CreateSessionOptions` | `memoryExtractionConfig` | `MemoryExtractionConfig` | 可省略 | 记忆抽取配置。 |
| `CreateSessionOptions` | `telemetry` / `extra` | `unknown` / `JsonObject` | 可省略 | 观测数据 / 扩展字段。 |
| `UpdateSessionConfigOptions` | `memoryExtractionConfig` | `MemoryExtractionConfig` | 可省略 | 替换或更新会话记忆抽取配置。 |
| `UpdateSessionConfigOptions` | `autoCommitPolicy` | `JsonObject \| null` | 属性不存在才省略 | 与 createSession 相同的三态 wire 语义。 |
| `UpdateSessionConfigOptions` | `telemetry` / `extra` | `unknown` / `JsonObject` | 可省略 | 观测数据 / 扩展字段。 |
| `MemoryExtractionConfig` | `events` | `{ tags?: string[] }` | 可省略 | 事件记忆抽取的子配置。 |
| `MemoryExtractionConfig.events` | `tags` | `string[]` | 可省略 | 自动抽取事件记忆默认附加的 Tags。 |
| `CommitSessionOptions` | `enableWorkingMemory` | `boolean \| null` | null 被 compact 省略 | 本次提交是否覆盖 Working Memory；null 继承会话策略。 |
| `CommitSessionOptions` | `keepRecentCount` | `number` | 可省略 | 旧兼容的最近消息保留数。 |
| `CommitSessionOptions` | `retentionMode` | `"turn_budget"` | 可省略 | 采用按轮保留预算的模式。 |
| `CommitSessionOptions` | `keepRecentTurnCount` | `number` | 可省略 | 需要保留的最近对话轮数。 |
| `CommitSessionOptions` | `retainedMessageTokenBudget` | `number` | 可省略 | 原始消息保留 token 预算。 |
| `CommitSessionOptions` | `minRawTailSteps` | `number` | 可省略 | 最少保留的原始尾部步骤。 |
| `CommitSessionOptions` | `eventTags` | `string[]` | undefined 不发，`[]` 显式发空 tags | 本次 commit 的事件 Tags，覆盖会话默认。 |
| `CommitSessionOptions` | `telemetry` / `extra` | `unknown` / `JsonObject` | 可省略 | 观测数据 / 扩展字段。 |
| `TaskListOptions` | `taskType` | `string` | 可省略 | 任务类型筛选。 |
| `TaskListOptions` | `status` | `string` | 可省略 | 任务状态筛选。 |
| `TaskListOptions` | `resourceId` | `string` | 可省略 | 关联资源 ID 筛选。 |
| `TaskListOptions` | `limit` | `number` | 可省略 | 返回任务数上限。 |
| `ListPage` | `result` | `unknown[]` | 服务端缺失时 `[]` | 当前页列表项；项无进一步 SDK 字段承诺。 |
| `ListPage` | `hasMore` | `boolean` | 服务端缺失时 `false` | 是否还有下一页。 |
| `TreePage` | `result` | `JsonObject[]` | 服务端缺失时 `[]` | 当前页树节点原样对象。 |
| `TreePage` | `hasMore` | `boolean` | 服务端缺失时 `false` | 是否还有下一页。 |
| `MatchedContext` | `uri` / `context_type` / `level` | `string` / `string` / `number` | 可省略 | 命中的资源 URI、上下文类别和层级。 |
| `MatchedContext` | `abstract` / `score` | `string` / `number` | 可省略 | 命中摘要和最终分数。 |
| `MatchedContext` | `origin_score` / `time_score` | `number \| null` | 可省略 | 原始相关性分和时间分。 |
| `MatchedContext` | `tags` | `string[]` | 可省略 | 命中的检索 Tags。 |
| `FindResult` | `memories` / `resources` / `skills` | `MatchedContext[]` | 可省略 | 分别按记忆、资源、技能分组的命中。 |
| `SearchContextEntry` | `uri` / `category` / `score` | `string` / `string` / `number` | 可省略 | 上下文条目的来源、类别和分数。 |
| `SearchContextEntry` | `detail` / `text` / `origin` | `string` | 可省略 | 可注入的详情、正文和来源描述。 |
| `SearchContextResult` | `entries` | `SearchContextEntry[]` | 可省略 | 组装后的上下文条目。 |
| `SearchContextResult` | `rendered` / `digest` | `string` | 可省略 | 可直接注入的渲染文本 / 摘要。 |
| `SearchContextResult` | `stats` | `JsonObject` | 可省略 | 上下文组装统计数据。 |
| `GitCommitOptions` | `message` | `string` | 必填 | 快照提交说明。 |
| `GitCommitOptions` | `paths` | `string[]` | 可省略 | 限定纳入快照的路径。 |
| `GitCommitOptions` | `branch` | `string` | `"main"` | 创建快照的分支。 |
| `GitCommitOptions` | `authorName` / `authorEmail` | `string` | 可省略 | 快照作者名称 / 邮箱。 |
| `GitRestoreOptions` | `sourceCommit` | `string` | 必填 | 恢复来源快照引用。 |
| `GitRestoreOptions` | `projectDir` | `string` | 可省略 | 要恢复的项目子目录。 |
| `GitRestoreOptions` | `branch` / `dryRun` | `string` / `boolean` | `"main"` / `false` | 恢复目标分支 / 是否仅试运行。 |
| `GitRestoreOptions` | `message` / `authorName` / `authorEmail` | `string` | 可省略 | 恢复提交的说明与作者署名。 |
| `GitBlob` | `oid` / `size` / `bytes` | `string` / `number` / `Uint8Array` | 二进制 gitShow 才返回 | 快照 blob ID、大小和原始字节。 |
| `ImportPackOptions` | `onConflict` | `"fail"\|"overwrite"\|"skip"` | 可省略 | 导入路径冲突时的策略。 |
| `ImportPackOptions` | `vectorMode` | `"auto"\|"recompute"\|"require"` | 可省略 | 导入时的向量处理策略。 |
| `ResolveAssetsOptions` | `catalogYaml` | `string` | 可省略 | 补充资产 catalog YAML。 |
| `ResolveAssetsOptions` | `manifestLabel` / `catalogLabel` | `string` | 可省略 | 错误/诊断中显示的两个来源标签。 |
| `ResolveAssetsOptions` | `extra` | `JsonObject` | 可省略 | 未建模 Assets 解析字段。 |
| `PreflightAssetOptions` | `branch` / `commit` | `string` | 可省略 | Git 资产要验证的分支 / 精确提交。 |
| `PreflightAssetOptions` | `authConfig` | `AssetGitAuth` | 可省略 | 本次预检使用的一次性 Git 凭据。 |
| `AssetGitAuth` | `username` / `token` | `string` | 可省略 | Git 用户名 / access token。 |
| `PreflightAssetOptions` | `extra` | `JsonObject` | 可省略 | 未建模预检字段。 |
| `ResponseEnvelope<T>` | `status` / `result` / `has_more` | `string` / `T` / `boolean` | 可省略 | transport 内部的状态、业务结果、分页标记。 |
| `ResponseEnvelope<T>` | `error` / `telemetry` / `profile` / `detail` | `APIErrorInfo` / `unknown` / `string[]` / `unknown` | 可省略 | 错误、观测、性能和附加详情。 |
| `APIErrorInfo` | `code` / `message` / `details` | `string` / `string` / `JsonObject` | 可省略 | 服务端错误码、说明与结构化详情。 |
| `OpenVikingError` | `name` | `string` | `"OpenVikingError"` | 错误类名称。 |
| `OpenVikingError` | `message` / `code` | `string` | code 默认 `"UNKNOWN"` | 人类可读错误说明 / 程序错误码。 |
| `OpenVikingError` | `details` / `statusCode` | `JsonObject` / `number \| undefined` | `{}` / 可省略 | 结构化错误详情 / HTTP 状态码。 |

#### 其余公开选项与内联 options 字段明细

| 类型或方法 | 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|---|
| `RequestOptions` | `signal` | `AbortSignal` | 可省略 | 取消支持此 options 的单个请求。 |
| `CompileOptions` | `instruction` | `string` | 可省略 | 编译技能的额外说明。 |
| `CompileOptions` | `args` | `JsonObject` | 空对象省略 | 编译技能的具名参数。 |
| `CompileOptions` | `extra` | `JsonObject` | 可省略 | 不可覆盖 instruction/args 的扩展字段。 |
| `GetSkillOptions` | `includeContent` | `boolean` | undefined 省略 | 是否返回技能正文。 |
| `GetSkillOptions` | `includeFiles` | `boolean` | `true` | 是否返回技能文件。 |
| `GetSkillOptions` | `includeSource` | `boolean` | `false` | 是否返回来源信息。 |
| `GetSkillOptions` | `level` | `number` | 可省略 | 指定读取技能层级。 |
| `GetSkillOptions` | `targetUri` | `string` | 可省略 | 技能查找目标根。 |
| `UpdateWatchOptions` | `watchInterval` | `number` | 可省略 | Watch 轮询间隔。 |
| `UpdateWatchOptions` | `isActive` | `boolean` | 可省略 | 是否启用 Watch。 |
| `UpdateWatchOptions` | `reason` | `string` | 可省略 | Watch 导入目的。 |
| `UpdateWatchOptions` | `instruction` | `string` | 可省略 | Watch 导入处理指令。 |
| `GrepOptions` | `caseInsensitive` | `boolean` | `false` | 是否忽略文本匹配大小写。 |
| `GrepOptions` | `nodeLimit` | `number` | `256` | grep 扫描节点上限。 |
| `GrepOptions` | `levelLimit` | `number` | 可省略 | grep 搜索资源层级上限。 |
| `GrepOptions` | `excludeUri` | `string` | 可省略 | grep 排除的 URI。 |
| `GrepOptions` | `tags` | `string[]` | 可省略 | grep 的标签筛选。 |
| `GrepOptions` | `includeTags` | `boolean` | 仅 true 发送 | grep 结果是否附带 tags。 |
| `GlobOptions` | `nodeLimit` | `number` | `256` | glob 扫描节点上限。 |
| `GlobOptions` | `tags` | `string[]` | 可省略 | glob 的标签筛选。 |
| `GlobOptions` | `includeTags` | `boolean` | 仅 true 发送 | glob 结果是否附带 tags。 |
| `BatchAddMessagesOptions` | `telemetry` | `unknown` | 可省略 | 整批消息的观测数据。 |
| `BatchAddMessagesOptions` | `extra` | `JsonObject` | 可省略 | 整批消息的扩展字段。 |
| `ExperienceTrajectoryOptions` | `limit` | `number` | 可省略 | 返回轨迹数上限。 |
| `ExperienceTrajectoryOptions` | `offset` | `number` | 可省略 | 轨迹分页偏移。 |
| `ExperienceTrajectoryOptions` | `startDate` | `string` | 可省略 | 轨迹日期筛选下界。 |
| `ExperienceTrajectoryOptions` | `endDate` | `string` | 可省略 | 轨迹日期筛选上界。 |
| `ExperienceOutcomeOptions` | `startDate` | `string` | 可省略 | outcome 聚合日期下界。 |
| `ExperienceOutcomeOptions` | `endDate` | `string` | 可省略 | outcome 聚合日期上界。 |
| `addSkill` 内联 options | `targetUri` / `extra` | `string` / `JsonObject` | 可省略 | 安装目标根 / 未建模安装字段；同时继承 WaitOptions。 |
| `updateSkill` 内联 options | `sourceMetadata` | `JsonObject` | 可省略 | 替换技能的新来源元数据。 |
| `updateSkill` 内联 options | `targetUri` / `extra` | `string` / `JsonObject` | 可省略 | 替换目标根 / 未建模替换字段；同时继承 WaitOptions。 |
| `listSkills` 内联 options | `nodeLimit` / `targetUri` | `number` / `string` | `1000` / 可省略 | 技能扫描上限 / 目标根筛选。 |
| `findSkills` 内联 options | `limit` / `scoreThreshold` | `number` / `number` | `10` / 可省略 | 返回数上限 / 最低相似度。 |
| `findSkills` 内联 options | `level` / `targetUri` / `telemetry` | `number[]` / `string` / `unknown` | 可省略 | 技能层级、目标根和观测数据。 |
| `validateSkill` 内联 options | `strict` / `sourcePath` | `boolean` / `string` | `false` / 可省略 | 严格校验开关 / 待校验内容来源路径。 |
| `validateSkill` 内联 options | `skillDirName` / `targetUri` | `string` / `string` | 可省略 | 技能目录名 / 校验目标根。 |
| `listWatches` 内联 options | `activeOnly` / `toUri` | `boolean` / `string` | `false` / 可省略 | 是否仅活跃 Watch / 按资源目标筛选。 |
| `remove` 内联 options | `recursive` / `wait` / `timeout` | `boolean` / `boolean` / `number` | `false` / `false` / 可省略 | 递归删除、等待删除完成和等待超时。 |
| `adminCreateAccount` 内联 options | `userConfig` / `seed` | `JsonObject` / `string` | 可省略 | 首位管理员配置 / 可复现密钥种子。 |
| `adminListAccounts` 内联 options | `name` / `limit` / `page` | `string` / `number` / `number` | 可省略；给 limit 时 page=1 | 通配账户名、页大小、页号。 |
| `adminRegisterUser` 内联 options | `userConfig` / `seed` | `JsonObject` / `string` | 可省略 | 注册用户配置 / 可复现密钥种子。 |
| `adminListUsers` 内联 options | `limit` / `name` / `role` / `page` | `number` / `string` / `string` / `number` | 可省略；给 limit 时 page=1 | 页大小、通配用户名、角色和页号。 |

#### 共享配置、选项与 ACL

| 类型 | 字段 | 类型/可选值 | 默认值与含义 |
|---|---|---|---|
| `ClientConfig` | `baseUrl` | `string` | 必填服务端地址；其余为可选：`apiKey`、`account`、`user`、`actorPeerId`、`timeout`、`headers`、`fetch`、`profile`、`uploadMode: "local" \| "shared"`。 |
| `WaitOptions` | `wait`, `timeout`, `telemetry` | `boolean`、`number`、`unknown` | 无共享默认；具体方法覆盖时以方法表为准。 |
| `AclSpec` | `acl_mode`, `entries` | `"inherit" \| "restricted"`；`{ principal, level }[]` | 每个 `level` 只能是 `read/write/manage`。 |
| `AddResourceOptions` | `acl`, `to`, `parent`, `createParent`, `reason`, `instruction`, `strict`, `ignoreDirs`, `include`, `exclude`, `directlyUploadMedia`, `preserveStructure`, `watchInterval`, `processingMode`, `args`, `tags`, `tagMode`, `extra` | 对应同名 camelCase TypeScript 字段；`processingMode` 为 `semantic_and_vectors \| vectors_only`，`tagMode` 为 `replace \| append \| clear` | 继承 `WaitOptions`；`to/parent` 互斥，tags/tagMode 的发送规则见 `addResource`。 |
| `WriteOptions` / `BatchWriteOptions` | `acl`, `mode`, `processingMode`, `tags`, `tagMode`, `extra` / `extra` | 写入专用字段，均可选 | 两者都继承 `WaitOptions`；`BatchWriteOperation` 为 `{ uri, content?, contentBase64?, mode?: "replace" \| "append" \| "create" \| "upsert" }`。 |
| `ReindexOptions` / `SetTagsOptions` | `mode`, `wait`, `force`, `recursive`, `tags`, `tagMode`, `extra` / `mode`, `recursive`, `telemetry`, `extra` | 都可选 | `reindex` 的实际默认见方法表；`setTags.mode` 默认 `replace`。 |
| `ImportPackOptions` | `onConflict`, `vectorMode` | `fail \| overwrite \| skip`；`auto \| recompute \| require` | 都可选。 |
| `RequestOptions` | `signal` | `AbortSignal` | 可取消支持该参数的方法。 |
| `GetSkillOptions` / `UpdateWatchOptions` | `includeContent`, `includeFiles`, `includeSource`, `level`, `targetUri` / `watchInterval`, `isActive`, `reason`, `instruction` | 都可选 | `getSkill` 的三个布尔默认值见方法表；Watch 更新只发送提供的字段。 |
| `GrepOptions` / `GlobOptions` | `caseInsensitive`, `nodeLimit`, `levelLimit`, `excludeUri`, `tags`, `includeTags` / `nodeLimit`, `tags`, `includeTags` | 都可选 | `grep/glob` 的 `nodeLimit` 默认 `256`；`includeTags=false` 时不发送。 |
| `ListOptions` | `simple`, `recursive`, `output`, `absLimit`, `includeAbstract`, `includeOverview`, `overviewLimit`, `showAllHidden`, `nodeLimit`, `offset`, `limit`, `sortBy`, `sortOrder`, `extraFields`, `tags`, `includeTags` | `sortBy=name\|mtime`；`sortOrder=asc\|desc`；`extraFields` 仅 `locked\|id\|count` | 默认值和只在 true 发送的字段见 `listPage`。 |
| `TreeOptions` | `output`, `absLimit`, `includeAbstract`, `includeOverview`, `overviewLimit`, `showAllHidden`, `directoriesOnly`, `nodeLimit`, `levelLimit`, `offset`, `limit`, `extraFields`, `tags`, `includeTags` | `extraFields` 仅 `locked\|id\|count` | 默认值和只在 true 发送的字段见 `treePage`。 |
| `CompileOptions` | `instruction?`, `args?`, `extra?` | `args: JsonObject` | 空 `args` 不发送。 |

#### 分页与检索结果

| 类型 | 稳定字段 | 契约 |
|---|---|---|
| `ListPage` | `result: unknown[]`, `hasMore: boolean` | SDK 把服务端 `result` 缺省为 `[]`，`has_more` 缺省为 `false`。列表项没有进一步字段承诺。 |
| `TreePage` | `result: JsonObject[]`, `hasMore: boolean` | 同上；每个树节点是未建模的原样对象。 |
| `MatchedContext` | `uri?`, `context_type?`, `level?`, `abstract?`, `score?`, `origin_score?`, `time_score?`, `tags?` | 单条检索命中；其余服务端字段允许存在但不构成 SDK 承诺。 |
| `FindResult` | `memories?`, `resources?`, `skills?` | 每项都是 `MatchedContext[]`；其他 result 字段不保证。 |
| `SearchContextEntry` | `uri?`, `category?`, `score?`, `detail?`, `text?`, `origin?` | 一个可注入上下文条目。 |
| `SearchContextResult` | `entries?`, `rendered?`, `digest?`, `stats?` | `entries` 为 `SearchContextEntry[]`，`stats` 为 `JsonObject`。 |
| `FindOptions` / `SearchOptions` | `targetUri`, `image`, `limit`, `nodeLimit`, `scoreThreshold`, `filter`, `contextType`, `telemetry`, `since`, `until`, `timeField`, `level`, `tags`, `includeProvenance`, `readContent`, `eventsTimeDecayProtection`, `extra`；另加 `sessionId`, `searchType` | 全部可选；`targetUri: string \| string[]`。`searchType` 只能是 `semantic \| keywords`；`null` 的 `eventsTimeDecayProtection` 会被 compact 删除。 |
| `SearchContextOptions` | `searchType`, `image`, `sessionId`, `limit`, `nodeLimit`, `scoreThreshold`, `filter`, `contextType`, `includeProvenance`, `tags`, `since`, `until`, `timeField`, `eventsTimeDecayProtection`, `queryExpansion`, `maxTokens`, `quotas`, `purpose`, `detail`, `dedupTurns`, `excludeUris`, `peerScope`, `otherPeerPenalty`, `rewrite`, `rewriteMaxBullets`, `telemetry`, `extra` | 全部可选；`queryExpansion=off\|auto`，`purpose=chat\|coding`，`peerScope=actor\|all`；nullable time-decay 字段的 `null` 不发送。 |

#### Session、消息与任务

| 类型 | 字段 | 契约 |
|---|---|---|
| `Message` | `role` | 必填 `string`。 |
|  | `content?`, `parts?` | 二者至少一个有效；非空 `parts: JsonObject[]` 时优先并抑制 `content`。 |
|  | `createdAt?`, `peerId?`, `turnId?`, `messageKind?`, `sourceMessageIds?`, `telemetry?` | `messageKind` 仅 `user_query \| assistant_step \| tool_transport \| checkpoint`。 |
| `CreateSessionOptions` | `sessionId?`, `memoryPolicy?`, `autoCommitPolicy?`, `memoryExtractionConfig?`, `telemetry?`, `extra?` | `autoCommitPolicy` 的“属性存在即发送（含 null）”语义见方法表。 |
| `UpdateSessionConfigOptions` | `memoryExtractionConfig?`, `autoCommitPolicy?`, `telemetry?`, `extra?` | 同样保留 `autoCommitPolicy` 的存在性语义。 |
| `MemoryExtractionConfig` | `events?.tags?` | 事件记忆提取 tags。 |
| `CommitSessionOptions` | `enableWorkingMemory?`, `keepRecentCount?`, `retentionMode?`, `keepRecentTurnCount?`, `retainedMessageTokenBudget?`, `minRawTailSteps?`, `eventTags?`, `telemetry?`, `extra?` | `retentionMode` 仅 `turn_budget`；`enableWorkingMemory` 允许 `boolean \| null`，但 `null` 在 wire 上会被删除并继承策略。 |
| `TaskListOptions` | `taskType?`, `status?`, `resourceId?`, `limit?` | 任务列表过滤 query；单个任务由 `getTask` 返回 `JsonObject \| null`，无稳定任务字段。 |
| `BatchAddMessagesOptions` | `telemetry?`, `extra?` | call-level 选项；批量消息对象不会逐项发送 `Message.telemetry`。 |
| `ExperienceTrajectoryOptions` / `ExperienceOutcomeOptions` | `limit`, `offset`, `startDate`, `endDate` / `startDate`, `endDate` | 都可选日期/分页 query。 |

#### 快照、Assets、响应与错误

| 类型 | 字段 | 契约 |
|---|---|---|
| `GitCommitOptions` | `message`, `paths?`, `branch?`, `authorName?`, `authorEmail?` | `message` 必填。 |
| `GitRestoreOptions` | `sourceCommit`, `projectDir?`, `branch?`, `dryRun?`, `message?`, `authorName?`, `authorEmail?` | `sourceCommit` 必填。 |
| `GitBlob` | `oid: string`, `size: number`, `bytes: Uint8Array` | 仅 `gitShow` 命中 octet-stream 分支时返回。 |
| `ResolveAssetsOptions` | `catalogYaml?`, `manifestLabel?`, `catalogLabel?`, `extra?` | Assets manifest 解析选项。 |
| `PreflightAssetOptions` | `branch?`, `commit?`, `authConfig?`, `extra?` | `authConfig` 为 `{ username?, token? }`；只用于本次 Git 预检请求。 |
| `ResponseEnvelope<T>` | `status?`, `result?`, `has_more?`, `error?`, `telemetry?`, `profile?`, `detail?` | transport 内部响应信封；业务方法通常已解包 `result`。`APIErrorInfo` 仅建模 `code?/message?/details?`。 |
| `OpenVikingError` | `name`, `message`, `code`, `details`, `statusCode` | SDK 抛出的错误：`name="OpenVikingError"`，`code` 默认 `"UNKNOWN"`，`details` 默认 `{}`，`statusCode` 可为 `undefined`。使用 `isOpenVikingError(error, code?)` 缩窄类型。 |

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
