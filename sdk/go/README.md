# OpenViking Go SDK

`github.com/volcengine/OpenViking/sdk/go` 是连接运行中 OpenViking Server 的 Go HTTP 客户端。本 README 是 Go SDK 唯一的使用文档：从安装、任务处理到 API 速查都在这里完成；服务端 HTTP 契约可再参考仓库的 API 文档。

## SDK 定位与支持范围

适用于 Go 服务、后端任务和基础设施程序通过 HTTP 使用 OpenViking。SDK 覆盖资源与技能、文件系统、检索、Session/Memory、后台任务、Watch、OVPack、ACL、状态观察和租户管理等公共能力。

SDK 不嵌入 OpenViking Server，也不包装 Prometheus metrics、控制台/debug 等仅供运维或服务端使用的路由。所有方法接收 `context.Context`，因此调用方可以统一控制取消和超时。

## 安装

```bash
go get github.com/volcengine/OpenViking/sdk/go
```

模块要求 Go 1.22 或更高版本，并需要一个可访问的 OpenViking HTTP 服务，例如 `http://127.0.0.1:1933`。

## 初始化、认证与配置

```go
package main

import (
    "context"
    "log"
    "time"

    openviking "github.com/volcengine/OpenViking/sdk/go"
)

func main() {
    client, err := openviking.NewClient(openviking.Config{
        BaseURL: "http://127.0.0.1:1933",
        APIKey:  "your-user-key",
        Timeout: 120 * time.Second,
    })
    if err != nil {
        log.Fatal(err)
    }
    defer client.CloseIdleConnections()

    _, _ = client.Health(context.Background())
}
```

| `Config` 字段 | 请求头 | 使用时机 |
|---|---|---|
| `APIKey` | `X-API-Key` | 普通 `api_key` 部署的必填认证信息 |
| `Account` | `X-OpenViking-Account` | trusted 部署或网关显式透传租户身份时使用 |
| `User` | `X-OpenViking-User` | 同上，指定用户身份 |
| `ActorPeerID` | `X-OpenViking-Actor-Peer` | 请求关联特定 actor peer 时使用 |

普通 API key 部署只需 `APIKey`，服务端会推导 account/user。Go SDK 不保留旧 `agent_id` 入口，请使用 `ActorPeerID`。

## 五分钟跑通：导入资源、等待任务并检索

资源导入默认异步。不要在拿到 `task_id` 后立即假定内容可以检索；应按任务 ID 轮询到 `completed`，并对 `failed`/`cancelled` 作为失败处理。

以下片段沿用上一节创建的 `client`，并额外导入 `fmt` 和 `time`。

```go
ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
defer cancel()

created, err := client.AddResource(ctx, "./docs/guide.md", &openviking.AddResourceOptions{
    To:     "viking://resources/demo-guide",
    Reason: "导入产品文档",
})
if err != nil {
    return err
}

taskID, _ := created["task_id"].(string)
for {
    task, err := client.GetTask(ctx, taskID)
    if err != nil {
        return err
    }
    switch task["status"] {
    case "completed":
        goto search
    case "failed", "cancelled":
        return fmt.Errorf("resource task %s ended with %v", taskID, task["status"])
    }
    time.Sleep(500 * time.Millisecond)
}

search:
result, err := client.Find(ctx, "产品文档", &openviking.FindOptions{
    TargetURI: "viking://resources/demo-guide",
    Limit:     5,
})
if err != nil {
    return err
}
fmt.Println(result)
```

若只需写入和向量化、不生成或刷新 `.abstract.md` / `.overview.md`，为 `AddResourceOptions` 或 `WriteOptions` 设置 `ProcessingMode: "vectors_only"`。需要完整语义处理时使用服务端默认值或相应的完整处理模式。

## 核心能力

### 资源、技能与 Watch

`AddResource` 接收本地文件、目录或 URL；`AddSkill` 导入技能目录或内容。二者返回后台任务。传入正数 `WatchInterval` 的资源导入会创建 Watch；使用 `ListWatches`、`UpdateWatch`、`TriggerWatch` 和 `DeleteWatch` 管理它。

```go
skill, err := client.AddSkill(ctx, "./skills/search-web", nil)
watches, err := client.ListWatches(ctx, &openviking.ListWatchesOptions{ActiveOnly: true})
_, _ = skill, watches
```

### 文件系统、内容、Tags 与 ACL

使用 `List`/`ListPage`、`Tree`/`TreePage`、`Stat`、`Read`、`DownloadBytes`、`Write`、`BatchWrite`、`Move` 和 `Remove` 访问资源树。`ListPage` 与 `TreePage` 返回分页元数据；需要持续取页时不要只使用旧的数组返回方法。

```go
page, err := client.ListPage(ctx, "viking://resources", &openviking.ListOptions{Limit: 50})
written, err := client.Write(ctx, "viking://resources/note.md", "# 笔记", &openviking.WriteOptions{
    Tags:    []string{"team=search", "source=go"},
    TagMode: "replace",
})
_, _ = page, written
```

`SetTags` 的有效模式是 `replace`、`append` 和 `clear`。Tags 是调用方提供的业务元数据，不应让模型推断。ACL 使用 `ACL`、`SetACL`、`SetACLMode`、`GrantACL`、`RevokeACL` 和 `DeleteACL`。

### 检索

`Find` 用于不带 Session 上下文的语义检索，`Search` 可带 Session 上下文；`SearchContext` 生成可直接注入 Agent 的上下文。`Grep` 与 `Glob` 用于文件内容和路径匹配。

`FindOptions.Image` 与 `SearchOptions.Image` 支持本地路径、`viking://`、`http(s)://` 和 `data:image` URI。本地图片由 SDK 转为 data URI；服务端需配置多模态 embedding 模型。

```go
result, err := client.Search(ctx, "红色海报风格", &openviking.SearchOptions{
    TargetURI: "viking://resources/images",
    Image:     "./poster.png",
    Limit:     5,
})
_ = result
```

### Session、Memory 与后台任务

使用 `CreateSession`、`AddMessage`/`BatchAddMessages`、`GetSessionContext` 和 `CommitSession` 管理会话。`CommitSessionOptions.EventTags` 可覆盖单次事件记忆 Tags；传空切片表示本次显式跳过 session 默认 Tags。

```go
_, err := client.CreateSession(ctx, &openviking.CreateSessionOptions{SessionID: "demo-session"})
_, err = client.AddMessage(ctx, "demo-session", "user", openviking.AddMessageOptions{
    Content: openviking.String("请记住我偏好简洁回答"),
})
commit, err := client.CommitSession(ctx, "demo-session", &openviking.CommitSessionOptions{
    EventTags: []string{"team=search"},
})
_ = commit
```

使用 `GetTask`、`ListTasks` 和 `CancelTask` 对任务做精确控制。`WaitProcessed` 只适合等待系统已有处理完成，不能替代“等待本次请求创建的任务”的轮询。

### OVPack、观察状态、资产与管理能力

- OVPack：`ExportOVPack`、`BackupOVPack` 将流写入本地文件；`ImportOVPack`、`RestoreOVPack` 返回导入任务。
- 状态：`Health`、`IsHealthy`、`GetStatus`、`QueueStatus`、`VikingDBStatus`、`ModelsStatus`、`CheckConsistency`。
- 资产：`ResolveOpenVikingAssets` 和 `PreflightOpenVikingAsset` 解析并预检 OpenViking Assets manifest。
- 管理：root key 可使用 `AdminCreateAccount`、用户/组管理、`AdminGetAccountSettings`、Agent Evolution 和 `AdminMigrate` 系列方法。

## 错误处理、超时、取消与易错点

服务端错误会以 `*openviking.Error` 返回。使用 `IsCode` 区分业务错误码；网络超时和取消则由传入的 context 决定。

```go
content, err := client.Read(ctx, "viking://resources/missing.md", 0, -1)
if err != nil {
    if openviking.IsCode(err, "NOT_FOUND") {
        // 按业务需要创建或忽略缺失文件。
    }
    return err
}
fmt.Println(content)
```

- 为一次调用设置截止时间时使用 `context.WithTimeout`；取消 context 会取消尚未完成的 HTTP 调用。
- `*bool`、`*int`、`*float64` 和 `*string` 字段用 `openviking.Bool`、`Int`、`Float64`、`String` 区分“显式零值”与“省略”。
- `ReindexOptions.Wait` 是 Go `bool`。只为设置 `Tags` 等字段传入非 nil options 时，零值会发送 `wait=false`；若需要同步重建，必须显式写 `Wait: true`。
- `WriteOptions.Wait` 与 `AddResourceOptions.Wait` 同理，异步任务必须获取并检查返回的 `task_id`。

## API 速查表

本节是当前 Go 实现的调用契约。每个公开 Client 方法都显式列在下表；`ctx context.Context` 始终必填，下面为可读性不重复列出。除明确说明的结构化返回外，`map[string]any` 与 `[]any` 都是服务端 `result` 的原样 JSON 映射：SDK **不承诺任何键、嵌套结构或值类型**，请按目标服务端版本解析。

### 方法契约

#### 资源、技能、Watch 与编译

| 方法 | 参数（必填性） | 默认/省略语义 | 返回类型与稳定关键字段 |
|---|---|---|---|
| `AddResource` | `path` 必填；`opts *AddResourceOptions` 可为 nil | `To` 与 `Parent` 不能同时给；本地普通文件/目录上传为临时文件（目录先压缩），URL、git/ssh 路径及不存在路径作为 `path` 传服务端；`Wait` 零值为 `false`，`DirectlyUploadMedia` nil 为 `true`；仅 `len(Args)>0` 发 `args`。 | `map[string]any` 原样结果，异步时通常由服务端给任务信息；不承诺键。 |
| `AddSkill` | `data any` 必填；`opts` 可 nil | 字符串若指向本地文件/目录则上传（目录 zip），否则作为数据；`Wait=false`；`TargetURI=nil` 省略。 | `map[string]any` 原样结果。 |
| `ListSkills` | `opts` 可 nil | `NodeLimit=0` 为 `1000`；`TargetURI=nil` 不发查询参数。 | `map[string]any` 原样结果。 |
| `FindSkills` | `queryText` 必填；`opts` 可 nil | `Limit=0` 为 `10`；空 `Level` 不发；指针阈值可显式为 0。 | `map[string]any` 原样结果。 |
| `ValidateSkill` | `data` 必填；`opts` 可 nil | nil 时 `strict=false`；非 nil options 会发送其 `Strict`（含 `false`）。 | `map[string]any` 原样结果。 |
| `GetSkill` | `skillName` 必填；`opts` 可 nil | `IncludeFiles` 默认 `true`；`IncludeContent`/`Level` 为 nil 则省略；nil options 仍发 `include_source=false`。 | `map[string]any` 原样结果。 |
| `UpdateSkill` | `skillName`、`data` 必填；`opts` 可 nil | 数据上传规则同 `AddSkill`；`Wait=false`；可选 map 为 nil 时省略。 | `map[string]any` 原样结果。 |
| `DeleteSkill` | `skillName` 必填；可变 `opts ...*DeleteSkillOptions` 可省略 | 仅读取第一个非 nil options；其 `TargetURI=nil` 省略。 | `map[string]any` 原样结果。 |
| `ListWatches` | `opts` 可 nil | 始终发送 `active_only`，nil/零值为 `false`；`ToURI` 非空时规范化。 | `map[string]any` 原样结果。 |
| `GetWatch` | `taskID` 必填；`toURI` 可空 | 空 `toURI` 不交叉校验；非空 URI 规范化。 | `map[string]any` 原样结果。 |
| `UpdateWatch` | 值类型 `opts UpdateWatchOptions` 必填 | `TaskID`、`ToURI` 至少一个；且 `WatchInterval`、`IsActive`、`Reason`、`Instruction` 至少一个非 nil，否则本地报错。指针可显式发送 0/false/空串。 | `map[string]any` 原样结果。 |
| `DeleteWatch`、`TriggerWatch` | `ref WatchRef` 必填 | `TaskID`、`ToURI` 至少一个，否则本地报错；两者都有时以 task 路由并附 `to_uri`；仅 ToURI 的 Trigger 使用集合 trigger 路由。 | `map[string]any` 原样结果。 |
| `Compile` | `fromURIs []string`、`to`、`skill` 必填；`opts` 可 nil | URI 会原样传入（此方法不规范化）；空 `Instruction` 省略，只有非空 `Args` 才发。 | `map[string]any` 原样结果（编译任务由服务端表达）。 |

#### 文件、Tags 与 ACL

| 方法 | 参数（必填性） | 默认/省略语义 | 返回类型与稳定关键字段 |
|---|---|---|---|
| `List` | `uri` 必填；`opts *ListOptions` 可 nil | 调用 `ListPage` 并丢弃分页信息。 | `[]any`：目录条目原样映射，不承诺条目键。 |
| `ListPage` | `uri` 必填；`opts` 可 nil | `Output="original"`、`AbsLimit=256`、`OverviewLimit=4000`、`NodeLimit=1000`；`Offset/Limit=0` 省略；`IncludeAbstract/Overview` 仅非 nil 发送。 | `*ListPage`：稳定字段 `Result []any`、`HasMore bool`。 |
| `Tree` | `uri` 必填；`opts *TreeOptions` 可 nil | 调用 `TreePage` 并丢弃分页信息。 | `[]map[string]any`：节点键不承诺。 |
| `TreePage` | `uri` 必填；`opts` 可 nil | `Output="original"`、`AbsLimit=128`、`OverviewLimit=4000`、`NodeLimit=1000`、`LevelLimit=nil` 为 `3`；分页 0 省略。 | `*TreePage`：稳定字段 `Result []map[string]any`、`HasMore bool`。 |
| `Stat`、`Attrs` | `uri` 必填 | URI 规范化。 | `map[string]any` 原样元数据，键不承诺。 |
| `Mkdir` | `uri` 必填；`description` 可空；可变 `acl ...ACLSpec` 可省略 | 只使用第一个 ACL；`description=""` 省略。 | `error`；成功无 result。 |
| `Remove` | `uri` 必填；`opts` 可 nil | 始终发送 `recursive`、`wait`，nil/零值均为 false；`Timeout=nil` 省略。 | `error`；成功无 result。 |
| `Move` | `fromURI`、`toURI` 必填 | 两者规范化。 | `error`；成功无 result。 |
| `Read` | `uri`、`offset`、`limit` 必填 | offset/limit 按所给 int 原样发送（`limit=-1` 也会发送）。 | `string` 内容。 |
| `DownloadBytes` | `uri` 必填 | 原始下载，非 2xx 解析为 `*Error`。 | `[]byte` 原始存储字节。 |
| `Abstract`、`Overview` | `uri` 必填 | URI 规范化。 | `string`，分别为 L0/L1 内容。 |
| `Write` | `uri`、`content` 必填；`opts` 可 nil | `Mode=""` 为 `replace`；`Wait=false` 会发送；Tags 仅非 nil 或 `TagMode="clear"` 时发送，非 nil 空切片发送 `[]`。 | `map[string]any` 原样结果。 |
| `BatchWrite` | `rootURI`、`operations []BatchWriteOperation` 必填；`opts` 可 nil | 所有 operation URI 与 root 规范化；`Wait *bool`：nil 省略，`Bool(false)` 明确发 false；每项 `Content`/`ContentBase64` 都是指针 omitempty。 | `map[string]any` 原样结果。 |
| `SetTags` | `uri`、`tags []string` 必填；`opts` 可 nil | nil tags 会被规范化为 `[]`；`Mode=""` 为 `replace`，此模式的空数组是 no-op；`clear` 才是删除已有 tags。 | `map[string]any` 原样结果。 |
| `Reindex` | `uri` 必填；`opts` 可 nil | **nil options** 使用 `Mode="vectors_only"`、`Wait=true`；传非 nil options 后 `Wait` 直接发送，Go 零值即 `false`。`Recursive=nil` 为 true；Tags 仅非 nil 或 `TagMode="clear"` 发送。 | `map[string]any` 原样结果。 |
| `ACL` | `uri` 必填 | URI 规范化。 | `map[string]any` 原样 ACL；SDK 不承诺 direct/inherited/effective 的具体键。 |
| `SetACL` | `uri`、`entries []ACLEntry` 必填；可变 `SetACLOptions` 可省略 | nil entries 变为 `[]`（清空 direct grants）；只读取首个 options，空 `ACLMode` 省略。 | `map[string]any` 原样结果。 |
| `SetACLMode` | `uri`、`aclMode` 必填 | URI 规范化，`aclMode` 原样发送。 | `map[string]any` 原样结果。 |
| `GrantACL` | `uri`、`principal`、`level` 必填 | URI 规范化。 | `map[string]any` 原样结果。 |
| `RevokeACL` | `uri`、`principal` 必填 | URI 规范化。 | `map[string]any` 原样结果。 |
| `DeleteACL` | `uri` 必填 | URI 规范化。 | `map[string]any` 原样结果。 |

#### 检索

| 方法 | 参数（必填性） | 默认/省略语义 | 返回类型与稳定关键字段 |
|---|---|---|---|
| `Find` | `queryText` 必填；`opts *FindOptions` 可 nil | 无 Session；`Limit=0` 为 10，若 `NodeLimit != nil` 则它覆盖 Limit（即使为 0）；`TargetURI=nil`、tags nil、指针字段 nil 均省略；本地图像文件转 data URI。 | `*FindResult`，字段见下表。 |
| `Search` | `queryText` 必填；`opts *SearchOptions` 可 nil | 同 `Find` 的 limit 和图像规则；`SessionID=""`、`SearchType=""` 省略。 | `*FindResult`。 |
| `SearchContext` | `query` 必填；`opts *SearchContextOptions` 可 nil | 请求固定 `mode="context"`；所有指针、nil map/slice 省略，本地图像规则同上。 | `*SearchContextResult`，字段见下表。 |
| `Grep` | `uri`、`pattern` 必填；`opts` 可 nil | `case_insensitive` 始终发送（零值 false）；tags nil 省略，`ExcludeURI` 非空时规范化。 | `map[string]any` 原样结果。 |
| `Glob` | `pattern` 必填；`uri` 可空；`opts` 可 nil | 空 uri 为 `viking://`；tags nil 省略。 | `map[string]any` 原样结果。 |

#### Session、消息与任务

| 方法 | 参数（必填性） | 默认/省略语义 | 返回类型与稳定关键字段 |
|---|---|---|---|
| `CreateSession` | `opts *CreateSessionOptions` 可 nil | 空 `SessionID` 省略；map 为 nil 省略；`DisableAutoCommit=true` 强制发送 `auto_commit_policy: null`，否则 nil policy 省略。 | `map[string]any` 原样结果。 |
| `ListSessions` | 无额外参数 | 无。 | `[]any` 原样条目，键不承诺。 |
| `GetSession` | `sessionID` 必填；`opts` 可 nil | 只在 `AutoCreate=true` 时发送查询参数。 | `map[string]any` 原样结果。 |
| `UpdateSessionConfig` | `sessionID` 必填；`opts` 可 nil | `MemoryExtractionConfig=nil` 省略；`AutoCommitPolicy *map[string]any`：nil 指针省略，非 nil 指针发送其值；`openviking.Map(nil)` 可明确发送 JSON `null`。 | `map[string]any` 原样结果。 |
| `SessionExists` | `sessionID` 必填 | `GetSession` 的 `NOT_FOUND`（包括 HTTP 404 的 `*Error`）转为 `(false, nil)`；其他错误保留。 | `bool`。 |
| `GetSessionContext` | `sessionID` 必填；`tokenBudget int` 必填 | `tokenBudget=0` 为 `128000`，其他值原样发送。 | `map[string]any` 原样上下文，键不承诺。 |
| `GetSessionArchive` | `sessionID`、`archiveID` 必填 | ID 作 URL path escape。 | `map[string]any` 原样 archive。 |
| `DeleteSession` | `sessionID` 必填 | 无。 | `error`；成功无 result。 |
| `AddMessage` | `sessionID`、`role`、值类型 `AddMessageOptions` 必填 | `len(Parts)>0` 优先于 `Content`；否则 Content 非 nil（可为 `String("")`）才发送；二者都无时本地报错。slice nil 省略，非 nil 空 slice 会发送。 | `map[string]any` 原样结果。 |
| `BatchAddMessages` | `sessionID`、`messages []Message` 必填；`opts` 可 nil | `messages` 按 `Message` 的 JSON `omitempty` 编码；无需本地内容校验。 | `map[string]any` 原样结果。 |
| `CommitSession` | `sessionID` 必填；`opts` 可 nil | 所有 `*int` nil 省略、可显式 0；`EventTags=nil` 不发 metadata，非 nil（包括 `[]`）发 `extraction_metadata.event.tags`。 | `map[string]any` 原样结果。 |
| `GetTask` | `taskID` 必填 | API `NOT_FOUND` 或状态码 404 返回 `(nil, nil)`；其余错误保留。 | `map[string]any` 原样任务；键（如 status）不承诺。 |
| `CancelTask` | `taskID` 必填 | 无。 | `map[string]any` 原样结果。 |
| `ListTasks` | `opts` 可 nil | 空字符串过滤条件省略，只有 `Limit>0` 才发送。 | `[]any` 原样任务条目，键不承诺。 |

#### OVPack、系统、资产、管理与演进

| 方法 | 参数（必填性） | 默认/省略语义 | 返回类型与稳定关键字段 |
|---|---|---|---|
| `ExportOVPack` | `uri`、`to` 必填；`opts` 可 nil | 写入本地：`to` 空为当前目录；目录会生成 `<uri 最后一段>.ovpack`，非 `.ovpack` 路径自动补后缀；父目录自动创建，先写同目录临时文件再 rename。 | `string` 本地输出路径。 |
| `BackupOVPack` | `to` 必填；`opts` 可 nil | 本地 I/O 同上，目录输出名 `openviking-backup.ovpack`；`IncludeVectors` 仅 true 时为 true。 | `string` 本地输出路径。 |
| `ImportOVPack` | 本地 `filePath`、`parent` 必填；`opts` 可 nil | 先读取并 multipart 上传本地文件；目录本地报错；`parent` 规范化；空 `OnConflict`/`VectorMode` 省略。 | `string`，稳定为响应 `uri`。 |
| `RestoreOVPack` | 本地 `filePath` 必填；`opts` 可 nil | 先上传；目录本地报错；可选字段规则同 Import。 | `string`，稳定为响应 `uri`。 |
| `WaitProcessed` | `opts` 可 nil | `Timeout *float64` nil 省略、非 nil 可显式 0。 | `map[string]any` 原样结果。 |
| `CheckConsistency` | `uri` 必填 | URI 规范化。 | `map[string]any` 原样结果。 |
| `Health` | 无额外参数 | 2xx 且 JSON `status=="ok"` 才 true；非 2xx 返回 `(false,nil)`。 | `bool`。 |
| `QueueStatus`、`VikingDBStatus`、`ModelsStatus`、`GetStatus` | 可变 `ObserverStatusOptions` 可省略 | 仅第一个 options 的非空 `Format` 生效。 | `map[string]any` 原样状态。 |
| `IsHealthy` | 无额外参数 | 调用 `GetStatus`，仅 `result["is_healthy"]` 为 bool true 时为 true；缺失/类型不同为 false。 | `bool`。 |
| `ResolveOpenVikingAssets` | `manifestYAML` 必填；`opts` 可 nil | 只有非 nil options 才处理 catalog/labels/extra；空 string 和 nil extra 均省略。 | `map[string]any` 原样结果。 |
| `PreflightOpenVikingAsset` | `name`、`repoURL` 必填；`opts` 可 nil | 固定 `connector="git"`；`AuthConfig=nil`、空 branch/commit 省略。 | `map[string]any` 原样结果。 |
| `AdminCreateAccount` | `accountID`、`adminUserID` 必填 | 等同 WithOptions(..., nil)。 | `map[string]any` 原样结果。 |
| `AdminCreateAccountWithOptions` | `accountID`、`adminUserID` 必填；`opts` 可 nil | `UserConfig=nil`、`Seed=nil` 省略；指针 Seed 可显式空串。 | `map[string]any` 原样结果。 |
| `AdminListAccounts` | 无额外参数 | 等同 WithOptions(nil)。 | `[]any` 原样账户条目。 |
| `AdminListAccountsWithOptions` | `opts` 可 nil | `Name` 空省略；只有 `Limit != nil` 才分页，Page nil 时随 Limit 发送 `page=1`。 | `[]any` 原样账户条目。 |
| `AdminDeleteAccount` | `accountID` 必填 | 无。 | `map[string]any` 原样结果。 |
| `AdminRegisterUser` | `accountID`、`userID`、`role` 必填 | role 空字符串会改为 `"user"`。 | `map[string]any` 原样结果。 |
| `AdminRegisterUserWithOptions` | `accountID`、`userID`、`role` 必填；`opts` 可 nil | role 空为 `"user"`；options 的 nil map/pointer 省略。 | `map[string]any` 原样结果。 |
| `AdminListUsers` | `accountID` 必填 | 等同 WithOptions(nil)。 | `[]any` 原样用户条目。 |
| `AdminListUsersWithOptions` | `accountID` 必填；`opts` 可 nil | `Name`/`Role` 空省略；分页规则同账户。 | `[]any` 原样用户条目。 |
| `AdminRemoveUser`、`AdminSetRole`、`AdminRegenerateKey` | 前两者：`accountID,userID`，SetRole 另需 `role`；Regenerate 同前两者，均必填 | `AdminRegenerateKey` 等同 WithOptions(nil)。 | `map[string]any` 原样结果。 |
| `AdminRegenerateKeyWithOptions` | `accountID`、`userID` 必填；`opts` 可 nil | 仅 `Seed != nil` 时有 body，值可为空串。 | `map[string]any` 原样结果。 |
| `AdminCreateGroup`、`AdminDeleteGroup`、`AdminListGroupMembers` | `accountID,groupID` 必填 | 无。 | 前两者 `map[string]any`；ListGroupMembers 也是 `map[string]any`（不是 `[]any`）。 |
| `AdminListGroups` | `accountID` 必填 | 无。 | `[]any` 原样组条目。 |
| `AdminAddGroupMember`、`AdminRemoveGroupMember` | `accountID,groupID,userID` 必填 | 无。 | `map[string]any` 原样结果。 |
| `AdminMigrate` | `opts` 可 nil | nil/`Cleanup=false` 发送 `action="migrate"`；true 为 `"cleanup"`。 | `map[string]any` 原样结果。 |
| `AdminGetAgentEvolution` | 无额外参数 | 无。 | `map[string]any` 原样设置。 |
| `AdminSetAgentEvolution` | `enabled bool` 必填 | false 也明确发送。 | `map[string]any` 原样结果。 |
| `AdminGetAccountSettings` | `accountID` 必填 | 无。 | `map[string]any` 原样有效/覆盖设置。 |
| `AdminSetAccountAgentEvolution` | `accountID`、`enabled` 必填 | false 也发送，嵌套于 `agent_evolution.enabled`。 | `map[string]any` 原样结果。 |
| `ListExperienceTrajectories` | `experienceURI` 必填；`opts` 可 nil | URI 规范化；指针 Limit/Offset nil 省略，date 空省略。 | `map[string]any` 原样结果。 |
| `GetExperienceOutcomes` | `experienceURI` 必填；`opts` 可 nil | URI 规范化；date 空省略。 | `map[string]any` 原样结果。 |
| `CloseIdleConnections` | 无额外参数 | 只关闭底层 HTTP client 的空闲连接；nil client 安全无操作。 | 无返回。 |

### 字段契约

#### `Config` 与通用省略规则

| 字段/类型 | 发送或生效规则 |
|---|---|
| `Config.BaseURL string` | 必填，去掉末尾 `/` 后必须有 scheme 与 host，否则 `NewClient` 返回错误。 |
| `APIKey`、`Account`、`User`、`ActorPeerID string` | 非空时分别写入 `X-API-Key`、`X-OpenViking-Account`、`X-OpenViking-User`、`X-OpenViking-Actor-Peer`。 |
| `Timeout time.Duration`、`HTTPClient *http.Client` | HTTPClient 非 nil 时直接使用（Timeout 忽略）；否则 Timeout=0 为 60 秒。 |
| `ExtraHeaders map[string]string`、`Profile bool`、`UploadMode string` | ExtraHeaders 复制后写入每个请求；Profile=true 附 `profile=1`；UploadMode 非空写入临时上传 multipart 字段。 |
| `*bool`、`*int`、`*float64`、`*string` | nil 通常表示省略，非 nil 区分显式 `false`/0/空串；用 `Bool`、`Int`、`Float64`、`String` 构造。具体非指针 bool 的发送规则以方法表为准。 |
| `map[string]any`、`Extra` | 多数 map 为 nil 时省略，非 nil 空 map 会发送 `{}`；`Extra` 中 nil 值跳过，且不能覆盖已有/受保护字段（会本地报错）。`Map(nil)` 只适用于 `*map[string]any` 字段，能把 JSON null 与省略区分开。 |
| `[]string` / slices | 大多数请求中 nil 省略、非 nil `[]` 明确发送空数组；Tags 的精确例外见 `SetTags`、`Write`、`Reindex` 和 `CommitSession`。 |
| URI | 除 `Compile` 外，URI 参数通常经 `NormalizeURI`：无 `viking://` 前缀时补上并去掉开头 `/`。 |

#### 分页、文件操作与 Tags Options

| 类型 | 字段 | 契约 |
|---|---|---|
| `ListOptions` | `Simple, Recursive, ShowAllHidden, IncludeTags bool` | ListPage 都会发送前三项（含 false）；IncludeTags 仅 true 发送。 |
|  | `Output, AbsLimit, OverviewLimit, NodeLimit` | 空/0 分别为 `original`、256、4000、1000。 |
|  | `Offset, Limit, SortBy, SortOrder, ExtraFields, Tags` | 前四项零值/空省略；slices nil 省略、非 nil 发送。 |
|  | `IncludeAbstract, IncludeOverview *bool` | nil 省略，指针可显式 false。 |
| `TreeOptions` | `Output, AbsLimit, OverviewLimit, NodeLimit, LevelLimit` | 默认 `original`、128、4000、1000、3；LevelLimit 指针可显式 0。其余字段的 bool/slice/分页语义同 List；`DirectoriesOnly` 仅 true 发送。 |
| `WriteOptions` | `ACL, Mode, Wait, Timeout, Telemetry, ProcessingMode, Tags, TagMode, Extra` | Mode 空为 replace；Wait 始终发（零值 false）；Timeout 指针；Tags/TagMode 的发送规则见方法表。 |
| `BatchWriteOperation` / `BatchWriteOptions` | `URI`；`Content *string`；`ContentBase64 *string`；`Mode`；`Wait *bool`、`Timeout *float64`、`Telemetry`、`Extra` | operation 的 Content/ContentBase64 nil 省略；Wait 使用指针，nil 和 false 不同。 |
| `RemoveOptions` / `ReindexOptions` | `Recursive, Wait, Timeout` / `Mode, Wait, Force, Recursive *bool, Tags, TagMode, Extra` | Remove 的 bool 始终作为 query 发送。Reindex 的 nil options 与非 nil options 的 Wait 差异必须按方法表处理；Force 仅 true 发送。 |
| `SetTagsOptions` / `ACLSpec` / `ACLEntry` | `Mode, Recursive, Telemetry, Extra` / `ACLMode, Entries` / `Principal, Level` | SetTags Mode 空为 replace。ACLSpec 的 Entries nil 表示由嵌入 API 省略，非 nil 空 slice 表示清空；ACLEntry 两字段按 JSON 必填编码。 |

#### 检索与上下文字段

| 类型 | 字段 | 契约 |
|---|---|---|
| `FindOptions`、`SearchOptions` | 共用 `TargetURI any, Image, Limit, NodeLimit, ScoreThreshold, Filter, ContextType, IncludeProvenance, ReadContent, Telemetry, Since, Until, TimeField, Level, Tags, Extra` | TargetURI 仅非 nil 发送，string 或 `[]string` 会规范化；Image 可为 data URI/http(s)/viking URI 或本地文件；本地文件读为 data URI。Limit 默认 10，`NodeLimit != nil` 覆盖；指针和 nil slice/map 省略。Search 另有 `SearchType, SessionID`，Find 另有 `EventsTimeDecayProtection`（Search 也有）。 |
| `SearchContextOptions` | `Image, SessionID, SearchType, Limit, NodeLimit, ScoreThreshold, Filter, ContextType, IncludeProvenance, Tags, Since, Until, TimeField, EventsTimeDecayProtection, QueryExpansion, MaxTokens, Quotas, Purpose, Detail, DedupTurns, ExcludeURIs, PeerScope, OtherPeerPenalty, Rewrite, RewriteMaxBullets, Telemetry, Extra` | 只将非空字符串、非 nil 指针/map/slice/interface 发往服务端；`mode=context` 不可由 options 改写。 |
| `FindResult` | `Memories, Resources, Skills []MatchedContext`；`QueryPlan *QueryPlan`；`QueryResults []map[string]any`；`Total int` | 这些是 SDK 声明的稳定字段；QueryResults 内 map 仍不承诺键，Total 缺失解为 0。 |
| `MatchedContext` | `URI, ContextType, Level, Abstract, Content, Overview, Category, OriginScore *float64, TimeScore *float64, Score, MatchReason, Tags` | 命中条目的稳定字段；OriginScore/TimeScore nil 表示未返回，Tags JSON 键为 `tags`。 |
| `SearchContextResult` | `Entries []SearchContextEntry`、`Rendered string`、`Digest string`、`Stats map[string]any` | Entries 的稳定字段为 `URI, Category, Score, Detail, Text, Origin`；Stats 的键不承诺。 |
| `GrepOptions` / `GlobOptions` | Grep：`CaseInsensitive, NodeLimit, LevelLimit, ExcludeURI, Tags, IncludeTags`；Glob：`NodeLimit, Tags, IncludeTags` | Grep 的 CaseInsensitive 总发送；其他指针/nil slice 省略；IncludeTags 只在 true 发送。 |

#### Session、消息、任务与错误字段

| 类型 | 字段 | 契约 |
|---|---|---|
| `CreateSessionOptions` | `SessionID, MemoryPolicy, AutoCommitPolicy, DisableAutoCommit, MemoryExtractionConfig, Telemetry, Extra` | 三个 map/interface nil 时省略；DisableAutoCommit=true 覆盖 AutoCommitPolicy 并发送 null。 |
| `GetSessionOptions` / `UpdateSessionConfigOptions` | `AutoCreate` / `MemoryExtractionConfig, AutoCommitPolicy *map[string]any, Telemetry, Extra` | AutoCreate 只有 true 发；`AutoCommitPolicy` 使用指针以区分 omit 与 null：`Map(nil)` 可发 null。 |
| `AddMessageOptions` / `Message` | `Content *string, Parts []map[string]any, CreatedAt, PeerID, TurnID, MessageKind, SourceMessageIDs, Telemetry` | AddMessage 要 Content 或非空 Parts；Message 仅按 JSON omitempty 编码，Content 可为 `String("")`，Parts nil/空都因 omitempty 不编码。 |
| `CommitSessionOptions` | `KeepRecentCount, KeepRecentTurnCount, RetainedMessageTokenBudget, MinRawTailSteps *int`；`RetentionMode, Telemetry, EventTags, Extra` | 指针 nil 省略、可显式 0；EventTags 的 nil 与 [] 区别见方法表。 |
| `ListTasksOptions` | `TaskType, Status, ResourceID string`；`Limit int` | 空条件、非正 Limit 均省略。 |
| `Error` | `Code string`、`Message string`、`Details map[string]any`、`StatusCode int` | 服务端/API 失败使用 `*Error`；Code 空时 Error() 只返回 Message。Details 的键不承诺；`IsCode(err, code)` 仅匹配此类型。 |
| `ErrorInfo` | `Code`、`Message`、`Details` | 响应 envelope 内部错误对象；会转换为 `*Error`，空 Code 变为 `UNKNOWN`，空 Message 变为 `Unknown error`。 |

## 兼容性、升级与测试

Go SDK 是独立 module，与服务端共同演进。升级时请查看 release notes，重点复核异步任务默认行为、Options 的省略/显式零值语义，以及服务端版本是否支持目标能力。不要将旧 `agent_id` 配置迁入 Go SDK；改用 `ActorPeerID`。

本地验证与可运行 smoke 示例：

```bash
cd sdk/go
go test ./...
go run ./examples/basic_usage
```

运行示例前，在 `examples/basic_usage/main.go` 顶部填入测试服务的 `baseURL` 和 `apiKey`。示例会覆盖资源、技能、Watch、Session、记忆提取和检索流程。
