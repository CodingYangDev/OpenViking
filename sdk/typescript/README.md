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

| 方法 | 参数 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|
| `addResource` | `source: string`, `options: AddResourceOptions = {}` | `source` 是存在的 Node 本地文件/目录时先上传（目录会打包）；否则按远程 URL 或服务端路径提交。`to` 与 `parent` 不可同时给出；`tags` 已给出时 `tagMode` 默认 `replace`，`tagMode: "clear"` 即使没有 `tags` 也发送。 | `Promise<JsonObject>` |
| `addSkill` | `source: unknown`, `options: WaitOptions & { targetUri?, extra? } = {}` | 仅字符串指向**存在的 Node 本地文件或目录**时上传；inline 内容和不存在路径的字符串都作为 `data` 发送。`wait` 默认 `false`。 | `Promise<JsonObject>` |
| `listSkills` | `options: { nodeLimit?, targetUri? } = {}` | `nodeLimit` 默认 `1000`。 | `Promise<JsonObject>` |
| `findSkills` | `query: string`, `options = {}` | `limit` 默认 `10`；可给 `scoreThreshold`、`level`、`targetUri`、`telemetry`。 | `Promise<JsonObject>` |
| `validateSkill` | `data: unknown`, `options = {}` | `strict` 默认 `false`；可给 `sourcePath`、`skillDirName`、`targetUri`。 | `Promise<JsonObject>` |
| `getSkill` | `name: string`, `options: GetSkillOptions = {}` | 未给 `includeContent` 时省略该 query；`includeFiles` 默认发送 `true`，`includeSource` 默认发送 `false`，显式 `false` 仍发送 `false`。 | `Promise<JsonObject>` |
| `updateSkill` | `name: string`, `source: unknown`, `options = {}` | 与 `addSkill` 相同：仅存在的 Node 本地文件/目录上传；inline 内容和不存在路径字符串作为 `data`。`wait` 默认 `false`，还可给 `sourceMetadata`、`targetUri`、`extra`。 | `Promise<JsonObject>` |
| `deleteSkill` | `name: string`, `targetUri?: string` | 未给 `targetUri` 时不带该 query。 | `Promise<JsonObject>` |
| `listWatches` | `options: { activeOnly?, toUri? } = {}` | `activeOnly` 默认 `false`；给出 `toUri` 时规范化 URI。 | `Promise<JsonObject>` |
| `getWatch` | `taskId: string`, `toUri?: string` | `toUri` 可选，给出时规范化。 | `Promise<JsonObject>` |
| `updateWatch` | `ref: { taskId?, toUri? }`, `changes: UpdateWatchOptions` | 至少提供一个引用，否则同步抛 `TypeError`；有 `taskId` 时优先按任务更新。 | `Promise<JsonObject>` |
| `deleteWatch` | `ref: { taskId?, toUri? }` | 至少提供一个引用，否则同步抛 `TypeError`；`taskId` 优先。 | `Promise<JsonObject>` |
| `triggerWatch` | `ref: { taskId?, toUri? }` | 至少提供一个引用，否则同步抛 `TypeError`；`taskId` 优先。 | `Promise<JsonObject>` |

#### 检索、目录与内容

| 方法 | 参数 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|
| `find` | `query: string`, `options: FindOptions = {}` | 不带 Session 的语义检索；本地 `image` 路径会转为 data URI。 | `Promise<FindResult>`；稳定字段见下表 |
| `search` | `query: string`, `options: SearchOptions = {}` | `sessionId` 与 `searchType` 仅此方法发送；本地 `image` 路径会转为 data URI。 | `Promise<FindResult>` |
| `searchContext` | `query: string`, `options: SearchContextOptions = {}` | 固定发送 `mode: "context"`；本地 `image` 路径会转为 data URI。 | `Promise<SearchContextResult>`；稳定字段见下表 |
| `grep` | `uri: string`, `pattern: string`, `options: GrepOptions = {}` | `caseInsensitive` 默认 `false`，`nodeLimit` 默认 `256`；`includeTags` 仅在 `true` 时发送。 | `Promise<JsonObject>` |
| `glob` | `pattern: string`, `uri = "viking://"`, `options: GlobOptions \| number = {}` | 数字第三参兼容为 `{ nodeLimit }`；`nodeLimit` 默认 `256`。 | `Promise<JsonObject>` |
| `list` | `uri: string`, `options: ListOptions = {}` | 调用 `listPage` 后只返回 `result`，会丢弃分页状态。 | `Promise<unknown[]>` |
| `listPage` | `uri: string`, `options: ListOptions = {}` | 默认：`simple/recursive/showAllHidden=false`、`output="original"`、`absLimit=256`、`overviewLimit=4000`、`nodeLimit=1000`。 | `Promise<ListPage>`；`result`、`hasMore` 稳定 |
| `tree` | `uri: string`, `options: TreeOptions = {}` | 调用 `treePage` 后只返回 `result`。 | `Promise<JsonObject[]>` |
| `treePage` | `uri: string`, `options: TreeOptions = {}` | 默认：`output="original"`、`absLimit=128`、`overviewLimit=4000`、`showAllHidden=false`、`nodeLimit=1000`、`levelLimit=3`；`directoriesOnly/includeTags` 仅为 `true` 才发送。 | `Promise<TreePage>`；`result`、`hasMore` 稳定 |
| `stat` / `attrs` | `uri: string` | URI 规范化。 | 各 `Promise<JsonObject>` |
| `mkdir` | `uri: string`, `description?: string`, `acl?: AclSpec` | 可选 body 字段按通用 compact 规则删除。 | `Promise<void>` |
| `read` | `uri: string`, `offset = 0`, `limit = -1` | 读取文本；`-1` 原样发送为默认 limit。 | `Promise<string>` |
| `downloadBytes` | `uri: string` | 下载二进制内容；服务端 JSON 错误仍解析并抛出。 | `Promise<Uint8Array>` |
| `abstract` / `overview` | `uri: string` | 分别读取 L0 / L1 文本内容。 | 各 `Promise<string>` |
| `write` | `uri: string`, `content: string`, `options: WriteOptions = {}` | 空字符串 `content` 仍发送，可用于清空文件；`tags` 已给出时 `tagMode` 默认 `replace`，`clear` 强制发送。 | `Promise<JsonObject>` |
| `batchWrite` | `rootUri: string`, `operations: BatchWriteOperation[]`, `options: BatchWriteOptions = {}` | 每个 `operation.uri` 规范化；`content` / `contentBase64` 的 `null` 或 `undefined` 被移除。 | `Promise<JsonObject>` |
| `setTags` | `uri: string`, `tags?: string[]`, `options: SetTagsOptions = {}` | `mode` 默认 `replace`，`recursive` 默认 `false`；`extra` 不能覆盖 `uri/tags/mode/recursive/telemetry`。 | `Promise<JsonObject>` |
| `reindex` | `uri: string`, `options: ReindexOptions = {}` | `mode="vectors_only"`、`wait=true`、`recursive=true`；`force` 只在 `true` 时发送。Tags 规则同 `write`；`extra` 不可覆盖 `force/tags/tag_mode`。 | `Promise<JsonObject>` |
| `remove` | `uri: string`, `options = {}` | `recursive=false`、`wait=false`；`timeout` 未给出则不带 query。 | `Promise<void>` |
| `move` | `fromUri: string`, `toUri: string` | 两端 URI 均规范化。 | `Promise<void>` |

#### ACL、Session、消息与任务

| 方法 | 参数 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|
| `aclGet` / `aclDelete` | `uri: string` | URI 规范化。 | 各 `Promise<JsonObject>` |
| `aclSet` | `uri: string`, `acl: AclSpec` | 将 `uri` 与 `acl` 顶层字段直接合并后发送。 | `Promise<JsonObject>` |
| `aclGrant` | `uri: string`, `principal: string`, `level: "read" \| "write" \| "manage"` | URI 规范化。 | `Promise<JsonObject>` |
| `aclRevoke` | `uri: string`, `principal: string` | URI 规范化。 | `Promise<JsonObject>` |
| `createSession` | `options: CreateSessionOptions = {}` | **存在性例外：**仅当对象拥有 `autoCommitPolicy` 属性时才发送 `auto_commit_policy`；该属性为 `undefined` 或 `null` 都发送 JSON `null`。未提供该属性则不发送。 | `Promise<JsonObject>` |
| `listSessions` | 无 | — | `Promise<unknown[]>` |
| `getSession` | `sessionId: string`, `autoCreate = false` | 仅 `autoCreate=true` 才发送 `auto_create=true`。 | `Promise<JsonObject>` |
| `updateSessionConfig` | `sessionId: string`, `options: UpdateSessionConfigOptions` | 与 `createSession` 相同的 `autoCommitPolicy` 存在性例外；可更新 `memoryExtractionConfig`。 | `Promise<JsonObject>` |
| `sessionExists` | `sessionId: string` | 将 `OpenVikingError` 的 `code="NOT_FOUND"` 转为 `false`，其他错误继续抛出。 | `Promise<boolean>` |
| `deleteSession` | `sessionId: string` | — | `Promise<void>` |
| `getSessionContext` | `sessionId: string`, `tokenBudget = 128_000` | `tokenBudget` 作为 `token_budget` query。 | `Promise<JsonObject>` |
| `getSessionArchive` | `sessionId: string`, `archiveId: string` | — | `Promise<JsonObject>` |
| `addMessage` | `sessionId: string`, `message: Message` | 必须有 `content` 或非空 `parts`，否则同步抛 `TypeError`；非空 `parts` 优先，`content` 不发送。 | `Promise<JsonObject>` |
| `batchAddMessages` | `sessionId: string`, `messages: Message[]`, `telemetry?: unknown` **或** `options?: BatchAddMessagesOptions` | 每条消息同 `addMessage` 验证/优先级；第三参为不含 `telemetry`/`extra` 的对象时按旧式 telemetry 值处理。 | `Promise<JsonObject>` |
| `commitSession` | `sessionId: string`, `options?: CommitSessionOptions` **或** `(keepRecentCount?: number, telemetry?: unknown, eventTags?: string[])` | 旧位置参数兼容。`enableWorkingMemory: null` 经 compact 删除、不在 wire 上发送，因此继承 Session 策略；`eventTags: undefined` 不发送 `extraction_metadata`，`[]` 则显式发送空 tags。 | `Promise<JsonObject>` |
| `getTask` | `taskId: string` | 服务端 `NOT_FOUND` 或 HTTP 404 转为 `null`；其他错误抛出。 | `Promise<JsonObject \| null>`；任务对象无 SDK 稳定键承诺 |
| `cancelTask` | `taskId: string` | — | `Promise<JsonObject>` |
| `listTasks` | `options: TaskListOptions = {}` | 可按 `taskType/status/resourceId/limit` 过滤。 | `Promise<unknown[]>` |
| `waitProcessed` | `timeout?: number` | 未给时 body 不含 `timeout`；是全局队列等待，不能替代特定 `taskId` 的状态检查。 | `Promise<JsonObject>` |

#### OVPack、编译、状态与快照

| 方法 | 参数 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|
| `exportOVPack` | `uri: string`, `to: string`, `includeVectors = false`, `options: RequestOptions = {}` | 响应流写入 **Node 本地** `to` 所指文件/目录；`signal` 可取消。 | `Promise<string>`：实际输出路径 |
| `backupOVPack` | `to: string`, `includeVectors = false`, `options: RequestOptions = {}` | 响应流写入 **Node 本地** restore-only 备份文件；`signal` 可取消。 | `Promise<string>`：实际输出路径 |
| `importOVPack` | `source: string`, `parent: string`, `options: ImportPackOptions = {}` | `source` 必须是存在的 **Node 本地文件**（目录/远程路径会抛 `TypeError`），上传后导入。 | `Promise<string>`：服务端返回的 `uri` |
| `restoreOVPack` | `source: string`, `options: ImportPackOptions = {}` | `source` 同样必须是存在的 **Node 本地文件**。 | `Promise<string>`：服务端返回的 `uri` |
| `compile` | `fromUris: string[]`, `to: string`, `skill: string`, `options: CompileOptions = {}` | `args` 为空对象时不发送；`extra` 不可覆盖 `instruction/args`。 | `Promise<JsonObject>` |
| `health` | `options: RequestOptions = {}` | 直接请求 `/health`；仅 HTTP 成功且 JSON `status === "ok"` 为真。 | `Promise<boolean>` |
| `checkConsistency` | `uri: string` | URI 规范化。 | `Promise<JsonObject>` |
| `getStatus` / `queueStatus` / `vikingDBStatus` / `modelsStatus` | `format?: "table" \| "json"` | 未给出则不带 `format` query。 | 各 `Promise<JsonObject>` |
| `isHealthy` | 无 | 仅检查 `await getStatus()` 的 `is_healthy === true`。 | `Promise<boolean>` |
| `gitCommit` | `options: GitCommitOptions` | `message` 必填；`branch` 默认 `"main"`。 | `Promise<JsonObject>` |
| `gitRestore` | `options: GitRestoreOptions` | `sourceCommit` 必填；`branch="main"`、`dryRun=false`。 | `Promise<JsonObject>` |
| `gitShow` | `targetRef: string`, `path?: string` | content type 为 `application/octet-stream` 时返回本地构造的 `GitBlob`，否则为原样 JSON。 | `Promise<JsonObject \| GitBlob>` |
| `gitLog` | `branch = "main"`, `limit = 20`, `paths?: string[]` | 按新到旧返回。 | `Promise<JsonObject[]>` |
| `gitDiff` | `path: string`, `toRef: string`, `fromRef?: string` | `path` 规范化。 | `Promise<JsonObject>` |
| `gitGetIgnore` | 无 | 服务端非字符串 result 被归一为 `""`。 | `Promise<string>` |
| `gitSetIgnore` | `content: string` | 设置账户 `.ovgitignore`。 | `Promise<void>` |
| `gitDeleteIgnore` | 无 | 删除账户 `.ovgitignore`。 | `Promise<void>` |

#### 管理、Experience 与 Assets

| 方法 | 参数 | 默认值与 wire 语义 | 返回 |
|---|---|---|---|
| `adminCreateAccount` | `accountId: string`, `adminUserId: string`, `options = {}` | `options` 可有 `userConfig`、`seed`。 | `Promise<JsonObject>` |
| `adminListAccounts` | `options = { name?, limit?, page? }` | `name` 支持 `*` / `?`；仅给出 `limit` 时才发送 page，且其默认值为 `1`。 | `Promise<unknown[]>` |
| `adminDeleteAccount` | `accountId: string` | — | `Promise<JsonObject>` |
| `adminRegisterUser` | `accountId: string`, `userId: string`, `role: string`, `options = {}` | `options` 可有 `userConfig`、`seed`。 | `Promise<JsonObject>` |
| `adminListUsers` | `accountId: string`, `options = { limit?, name?, role?, page? }` | `name` 支持 `*` / `?`；`page` 规则同 `adminListAccounts`。 | `Promise<unknown[]>` |
| `adminRemoveUser` | `accountId: string`, `userId: string` | — | `Promise<JsonObject>` |
| `adminSetRole` | `accountId: string`, `userId: string`, `role: string` | — | `Promise<JsonObject>` |
| `adminRegenerateKey` | `accountId: string`, `userId: string`, `seed?: string` | `seed` 为 `undefined` 时请求无 body；给值才发送。 | `Promise<JsonObject>` |
| `adminMigrate` | `cleanup = false` | `false` 发送 `action: "migrate"`，`true` 发送 `"cleanup"`。 | `Promise<JsonObject>` |
| `adminGetAgentEvolution` | 无 | 当前调用方账户的有效开关。 | `Promise<JsonObject>` |
| `adminSetAgentEvolution` | `enabled: boolean` | 持久化并热加载当前账户开关。 | `Promise<JsonObject>` |
| `adminGetAccountSettings` | `accountId: string` | 返回该账户有效与显式覆盖设置的原样映射。 | `Promise<JsonObject>` |
| `adminSetAccountAgentEvolution` | `accountId: string`, `enabled: boolean` | 更新该账户 allowlist 中的 Agent Evolution 设置。 | `Promise<JsonObject>` |
| `listExperienceTrajectories` | `experienceUri: string`, `options: ExperienceTrajectoryOptions = {}` | URI 规范化；`limit/offset/startDate/endDate` 都是可选 query。 | `Promise<JsonObject>` |
| `getExperienceOutcomes` | `experienceUri: string`, `options: ExperienceOutcomeOptions = {}` | URI 规范化；可按日期区间过滤。 | `Promise<JsonObject>` |
| `resolveOpenVikingAssets` | `manifestYaml: string`, `options: ResolveAssetsOptions = {}` | `catalogYaml/manifestLabel/catalogLabel` 可选；`extra` 不可覆盖已发送字段。 | `Promise<JsonObject>` |
| `preflightOpenVikingAsset` | `name: string`, `repoUrl: string`, `options: PreflightAssetOptions = {}` | 固定发送 `connector: "git"`；可给 `branch/commit/authConfig`。 | `Promise<JsonObject>` |

### 字段契约

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
