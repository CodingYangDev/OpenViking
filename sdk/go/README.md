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

#### 方法参数含义

`ctx context.Context` 对所有方法均为必填，负责请求取消与截止时间。下表是方法表的参数语义索引；`opts` 的每个字段含义、默认值和省略规则见后面的“逐字段业务含义”。

| 方法 | 参数 | 含义 |
|---|---|---|
| `AddResource` | `path` 是本地文件、目录或交给服务端解析的 URL/git/ssh 路径。 | `AddResourceOptions` 决定导入目标、处理模式、过滤、ACL、tags、Watch 和同步等待。 |
| `AddSkill` | `data` 是技能内容，或可上传的本地技能文件/目录。 | `AddSkillOptions` 决定目标 skills 根、等待、超时和遥测。 |
| `ListSkills` | 无。 | `ListSkillsOptions` 限制节点数并选择 skills 根。 |
| `FindSkills` | `queryText` 是用于语义匹配技能的查询。 | `FindSkillsOptions` 控制数量、阈值、层级、根和遥测。 |
| `ValidateSkill` | `data` 是待校验的技能内容或本地来源。 | `ValidateSkillOptions` 控制严格校验、来源信息和 skills 根。 |
| `GetSkill` | `skillName` 是技能标识。 | `GetSkillOptions` 决定是否取正文、文件、来源、层级及根。 |
| `UpdateSkill` | `skillName` 标识被更新技能；`data` 是新内容/本地来源。 | `UpdateSkillOptions` 决定等待、来源元数据、目标根和扩展参数。 |
| `DeleteSkill` | `skillName` 是要删除的技能标识。 | 首个 `DeleteSkillOptions` 限定删除的 skills 根。 |
| `ListWatches` | 无。 | `ListWatchesOptions` 按启用状态或目标 URI 过滤。 |
| `GetWatch` | `taskID` 定位 Watch；`toURI` 可用于目标交叉定位。 | 无。 |
| `UpdateWatch` | 值类型 `UpdateWatchOptions` 同时提供 Watch 标识和要更新的字段。 | 指针字段让 0/false/空串成为明确的更新值。 |
| `DeleteWatch` | `ref` 按 task ID 或目标 URI 定位要删的 Watch。 | 无。 |
| `TriggerWatch` | `ref` 按 task ID 或目标 URI 定位要立即触发的 Watch。 | 无。 |
| `Compile` | `fromURIs` 是输入资源；`to` 是输出 URI；`skill` 是执行编译的技能名。 | `CompileOptions` 提供指令、技能参数与扩展参数。 |
| `List` | `uri` 是待列举目录。 | `ListOptions` 控制分页、层级、返回内容、排序和 tags 过滤。 |
| `ListPage` | `uri` 是待列举目录。 | `ListOptions` 同 `List`，但返回 `HasMore` 供翻页。 |
| `Tree` | `uri` 是树根目录。 | `TreeOptions` 控制深度、节点、分页、字段和 tags 过滤。 |
| `TreePage` | `uri` 是树根目录。 | `TreeOptions` 同 `Tree`，但返回 `HasMore` 供翻页。 |
| `Stat` | `uri` 是要查看元数据的节点。 | 无。 |
| `Attrs` | `uri` 是要查看属性的节点。 | 无。 |
| `Mkdir` | `uri` 是新目录 URI；`description` 是可选目录说明。 | 首个 `ACLSpec` 可在创建时设置 ACL。 |
| `Remove` | `uri` 是要删除的节点或子树。 | `RemoveOptions` 控制递归、等待和超时。 |
| `Move` | `fromURI` 是原 URI；`toURI` 是目标 URI。 | 无。 |
| `Read` | `uri` 是文件；`offset` 是读取起点；`limit` 是长度（可为 `-1`）。 | 无。 |
| `DownloadBytes` | `uri` 是要原样下载的文件。 | 无。 |
| `Abstract` | `uri` 是要读 L0 摘要的节点。 | 无。 |
| `Overview` | `uri` 是要读 L1 概览的节点。 | 无。 |
| `Write` | `uri` 是目标文件；`content` 是写入文本。 | `WriteOptions` 控制写入模式、处理、ACL、tags、等待和扩展参数。 |
| `BatchWrite` | `rootURI` 是批量写入根；`operations` 是各文件操作。 | `BatchWriteOptions` 控制整批等待、超时、遥测和扩展参数。 |
| `SetTags` | `uri` 是目标；`tags` 是要设置的业务标签。 | `SetTagsOptions` 控制 replace/append/clear、递归和遥测。 |
| `Reindex` | `uri` 是要重建的节点/子树。 | `ReindexOptions` 控制模式、同步等待、递归、强制和 tags；传非 nil opts 时需显式 `Wait:true` 才同步。 |
| `ACL` | `uri` 是要读取 ACL 的节点。 | 无。 |
| `SetACL` | `uri` 是目标；`entries` 是完整直接授权列表。 | 首个 `SetACLOptions` 可原子设置 ACL mode。 |
| `SetACLMode` | `uri` 是目标；`aclMode` 是新的继承/受限模式。 | 无。 |
| `GrantACL` | `uri` 是目标；`principal` 是用户/组；`level` 是权限等级。 | 无。 |
| `RevokeACL` | `uri` 是目标；`principal` 是要移除授权的用户/组。 | 无。 |
| `DeleteACL` | `uri` 是要清除直接 ACL 和受限模式的节点。 | 无。 |
| `Find` | `queryText` 是无 Session 语义检索查询。 | `FindOptions` 控制范围、图片、过滤、分数、时间、层级、tags 和返回内容。 |
| `Search` | `queryText` 是可带 Session 的语义检索查询。 | `SearchOptions` 同 Find，另提供 Session 和搜索类型。 |
| `SearchContext` | `query` 是为 Agent 组装上下文的查询。 | `SearchContextOptions` 控制检索、配额、渲染、去重、改写和 peer 范围。 |
| `Grep` | `uri` 是搜索根；`pattern` 是内容匹配模式。 | `GrepOptions` 控制大小写、深度、排除 URI 和 tags。 |
| `Glob` | `pattern` 是路径匹配模式；`uri` 可指定搜索根。 | `GlobOptions` 控制节点数和 tags。 |
| `CreateSession` | 无。 | `CreateSessionOptions` 提供会话 ID、记忆和自动提交策略。 |
| `ListSessions` | 无。 | 无。 |
| `GetSession` | `sessionID` 是会话标识。 | `GetSessionOptions.AutoCreate` 决定缺失时是否自动创建。 |
| `UpdateSessionConfig` | `sessionID` 是会话标识。 | `UpdateSessionConfigOptions` 修改记忆提取和自动提交策略。 |
| `SessionExists` | `sessionID` 是待检查会话。 | 无。 |
| `GetSessionContext` | `sessionID` 是会话；`tokenBudget` 是上下文 token 上限。 | 无。 |
| `GetSessionArchive` | `sessionID` 是会话；`archiveID` 是归档标识。 | 无。 |
| `DeleteSession` | `sessionID` 是要删除的会话。 | 无。 |
| `AddMessage` | `sessionID` 是会话；`role` 是消息角色；`opts` 承载内容。 | `AddMessageOptions` 提供文本/多段内容及 peer、轮次、类型、来源和遥测。 |
| `BatchAddMessages` | `sessionID` 是会话；`messages` 是要追加的消息序列。 | `BatchAddMessagesOptions` 仅提供批次遥测和扩展参数。 |
| `CommitSession` | `sessionID` 是要提交的会话。 | `CommitSessionOptions` 控制保留策略及本次事件记忆 tags。 |
| `GetTask` | `taskID` 是后台任务标识。 | 无。 |
| `CancelTask` | `taskID` 是要取消的任务标识。 | 无。 |
| `ListTasks` | 无。 | `ListTasksOptions` 按类型、状态、资源和数量过滤。 |
| `ExportOVPack` | `uri` 是导出根；`to` 是本地输出文件或目录。 | `PackOptions` 决定是否包含向量。 |
| `BackupOVPack` | `to` 是本地输出文件或目录。 | `PackOptions` 决定是否包含向量。 |
| `ImportOVPack` | `filePath` 是本地包文件；`parent` 是导入父 URI。 | `ImportPackOptions` 控制冲突与向量导入策略。 |
| `RestoreOVPack` | `filePath` 是本地备份包文件。 | `ImportPackOptions` 控制冲突与向量恢复策略。 |
| `WaitProcessed` | 无。 | `WaitProcessedOptions.Timeout` 控制等待系统已有处理工作的时长。 |
| `CheckConsistency` | `uri` 是要检查一致性的根。 | 无。 |
| `Health` | 无。 | 无。 |
| `QueueStatus` | 无。 | 首个 `ObserverStatusOptions` 控制队列状态格式。 |
| `VikingDBStatus` | 无。 | 首个 `ObserverStatusOptions` 控制数据库状态格式。 |
| `ModelsStatus` | 无。 | 首个 `ObserverStatusOptions` 控制模型状态格式。 |
| `GetStatus` | 无。 | 首个 `ObserverStatusOptions` 控制总状态格式。 |
| `IsHealthy` | 无。 | 无。 |
| `ResolveOpenVikingAssets` | `manifestYAML` 是待解析的 Assets manifest。 | `ResolveAssetsOptions` 追加 catalog、标签和扩展参数。 |
| `PreflightOpenVikingAsset` | `name` 是资产名；`repoURL` 是 Git 仓库 URL。 | `PreflightAssetOptions` 指定分支/commit 和一次性 Git 认证。 |
| `AdminCreateAccount` | `accountID` 是新账户；`adminUserID` 是初始管理员。 | 无；等同 WithOptions 的 nil options。 |
| `AdminCreateAccountWithOptions` | `accountID` 是新账户；`adminUserID` 是初始管理员。 | `AdminCreateAccountOptions` 提供管理员配置和 key seed。 |
| `AdminListAccounts` | 无。 | 无；等同 WithOptions 的 nil options。 |
| `AdminListAccountsWithOptions` | 无。 | `AdminListAccountsOptions` 按通配名称筛选并可分页。 |
| `AdminDeleteAccount` | `accountID` 是要删除的账户。 | 无。 |
| `AdminRegisterUser` | `accountID` 是账户；`userID` 是新用户；`role` 是其角色。 | 无；空 role 由 SDK 改为 `user`。 |
| `AdminRegisterUserWithOptions` | `accountID` 是账户；`userID` 是新用户；`role` 是其角色。 | `AdminRegisterUserOptions` 提供用户配置和 key seed。 |
| `AdminListUsers` | `accountID` 是目标账户。 | 无；等同 WithOptions 的 nil options。 |
| `AdminListUsersWithOptions` | `accountID` 是目标账户。 | `AdminListUsersOptions` 按名称/角色筛选并可分页。 |
| `AdminRemoveUser` | `accountID` 是账户；`userID` 是要移除的用户。 | 无。 |
| `AdminSetRole` | `accountID` 是账户；`userID` 是用户；`role` 是新角色。 | 无。 |
| `AdminRegenerateKey` | `accountID` 是账户；`userID` 是要换 key 的用户。 | 无；等同 WithOptions 的 nil options。 |
| `AdminRegenerateKeyWithOptions` | `accountID` 是账户；`userID` 是要换 key 的用户。 | `AdminRegenerateKeyOptions.Seed` 控制新 key 的种子。 |
| `AdminCreateGroup` | `accountID` 是账户；`groupID` 是新组 ID。 | 无。 |
| `AdminListGroups` | `accountID` 是目标账户。 | 无。 |
| `AdminDeleteGroup` | `accountID` 是账户；`groupID` 是要删除的组。 | 无。 |
| `AdminListGroupMembers` | `accountID` 是账户；`groupID` 是要查看的组。 | 无。 |
| `AdminAddGroupMember` | `accountID` 是账户；`groupID` 是组；`userID` 是待加入用户。 | 无。 |
| `AdminRemoveGroupMember` | `accountID` 是账户；`groupID` 是组；`userID` 是待移除用户。 | 无。 |
| `AdminMigrate` | 无。 | `AdminMigrateOptions.Cleanup` 在迁移与清理之间选择。 |
| `AdminGetAgentEvolution` | 无。 | 无。 |
| `AdminSetAgentEvolution` | `enabled` 是全局 Agent Evolution 开关，false 也会发送。 | 无。 |
| `AdminGetAccountSettings` | `accountID` 是要读取有效设置的账户。 | 无。 |
| `AdminSetAccountAgentEvolution` | `accountID` 是账户；`enabled` 是该账户开关，false 也会发送。 | 无。 |
| `ListExperienceTrajectories` | `experienceURI` 是 experience 资源根。 | `ExperienceTrajectoryOptions` 控制日期范围、limit 和 offset。 |
| `GetExperienceOutcomes` | `experienceURI` 是 experience 资源根。 | `ExperienceOutcomeOptions` 控制 outcome 的日期范围。 |
| `CloseIdleConnections` | 无。 | 无；关闭底层 HTTP client 的空闲连接。 |

### 字段契约

#### 逐字段业务含义

每个 Options、配置和结构化结果字段均单列；未特别写明时，`string` 的空值、`map`/slice 的 nil 都省略，`Extra` 的 nil value 会跳过且不能覆盖 SDK 已设置的保留字段。

##### `Config`、ACL、Watch 与文件 Options

| 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|
| `Config.BaseURL` | `string` | 必填；末尾 `/` 移除后必须有 scheme/host | 服务端 HTTP 根地址。 |
| `Config.APIKey` | `string` | 空则不发 | 普通部署的认证 key（`X-API-Key`）。 |
| `Config.Account` | `string` | 空则不发 | trusted/网关部署显式透传的账户（`X-OpenViking-Account`）。 |
| `Config.User` | `string` | 空则不发 | trusted/网关部署显式透传的用户（`X-OpenViking-User`）。 |
| `Config.ActorPeerID` | `string` | 空则不发 | 请求关联的 actor peer（`X-OpenViking-Actor-Peer`）。 |
| `Config.Timeout` | `time.Duration` | 未给 `HTTPClient` 时，0 为 60 秒 | SDK 自建 HTTP client 的请求超时。 |
| `Config.ExtraHeaders` | `map[string]string` | nil 不加 | 全请求附加 HTTP 头。 |
| `Config.HTTPClient` | `*http.Client` | 非 nil 时采用它并忽略 `Timeout` | 调用方控制 transport/超时的 client。 |
| `Config.Profile` | `bool` | false 不加 | true 时请求 `profile=1` 性能剖析。 |
| `Config.UploadMode` | `string` | 空则不发 | 临时 multipart 上传模式。 |
| `AddResourceOptions.ACL` | `*ACLSpec` | nil 省略 | 导入时一并设置资源 ACL。 |
| `AddResourceOptions.To` | `string` | 空省略；不能与 Parent 同给 | 导入后的完整目标 URI。 |
| `AddResourceOptions.Parent` | `string` | 空省略；不能与 To 同给 | 由服务端命名时的目标父目录。 |
| `AddResourceOptions.CreateParent` | `*bool` | nil 省略 | 是否自动创建缺失父目录。 |
| `AddResourceOptions.AddType` | `string` | 空省略 | 服务端资源导入类型。 |
| `AddResourceOptions.Reason` | `string` | 空省略 | 记录导入的业务原因。 |
| `AddResourceOptions.Instruction` | `string` | 空省略 | 资源语义处理的附加指令。 |
| `AddResourceOptions.Wait` | `bool` | 总发送；零值 false | 是否等待导入任务完成。 |
| `AddResourceOptions.Timeout` | `*float64` | nil 省略 | 导入等待超时。 |
| `AddResourceOptions.Strict` | `bool` | 总发送；零值 false | 是否启用严格导入处理。 |
| `AddResourceOptions.IgnoreDirs` | `string` | 空省略 | 导入目录时忽略的目录规则。 |
| `AddResourceOptions.Include` | `string` | 空省略 | 导入目录时纳入的文件规则。 |
| `AddResourceOptions.Exclude` | `string` | 空省略 | 导入目录时排除的文件规则。 |
| `AddResourceOptions.DirectlyUploadMedia` | `*bool` | nil 时 SDK 发 true | 是否直接上传媒体文件。 |
| `AddResourceOptions.PreserveStructure` | `*bool` | nil 省略 | 是否保留目录/压缩包层级。 |
| `AddResourceOptions.WatchInterval` | `float64` | 0 省略 | 正数时创建 Watch 的轮询间隔。 |
| `AddResourceOptions.ProcessingMode` | `string` | 空省略 | 后处理模式，例如 `vectors_only`。 |
| `AddResourceOptions.Args` | `map[string]any` | 仅 `len>0` 发送 | 传给资源处理器的扩展参数。 |
| `AddResourceOptions.Tags` | `[]string` | nil 省略，空数组发送 | 导入资源的业务标签。 |
| `AddResourceOptions.TagMode` | `string` | 空省略 | 导入时处理 tags 的方式。 |
| `AddResourceOptions.Telemetry` | `any` | nil 省略 | 导入遥测。 |
| `AddResourceOptions.Extra` | `map[string]any` | nil 省略 | 导入透传扩展参数。 |
| `ACLEntry.Principal` | `string` | JSON 必填 | 被直接授予权限的用户或组。 |
| `ACLEntry.Level` | `string` | JSON 必填 | 授予 principal 的权限等级。 |
| `ACLSpec.ACLMode` | `string` | 空省略 | 同时设置的继承/受限 ACL 模式。 |
| `ACLSpec.Entries` | `[]ACLEntry` | 嵌入时 nil 省略，非 nil 空数组清空 | 同时替换的直接 ACL 条目。 |
| `SetACLOptions.ACLMode` | `string` | 空省略；只读首个 options | `SetACL` 时原子更新的 ACL 模式。 |
| `WatchRef.TaskID` | `string` | 与 `ToURI` 至少一个非空 | 要操作的 Watch 任务标识。 |
| `WatchRef.ToURI` | `string` | 与 `TaskID` 至少一个非空 | 要操作/校验的 Watch 资源目标。 |
| `UpdateWatchOptions.TaskID` | `string` | 与 `ToURI` 至少一个非空 | 被修改的 Watch 任务。 |
| `UpdateWatchOptions.ToURI` | `string` | 与 `TaskID` 至少一个非空 | 被修改的 Watch 目标 URI。 |
| `UpdateWatchOptions.WatchInterval` | `*float64` | nil 不更新；可显式 0 | 新的 Watch 扫描间隔。 |
| `UpdateWatchOptions.IsActive` | `*bool` | nil 不更新；可显式 false | 启用或暂停 Watch。 |
| `UpdateWatchOptions.Reason` | `*string` | nil 不更新；可显式空串 | 更新 Watch 的业务原因。 |
| `UpdateWatchOptions.Instruction` | `*string` | nil 不更新；可显式空串 | Watch 触发时使用的处理指令。 |
| `ListWatchesOptions.ActiveOnly` | `bool` | 总发送；零值 false | 是否只查询启用的 Watch。 |
| `ListWatchesOptions.ToURI` | `string` | 空省略 | 按目标 URI 筛选 Watch。 |
| `ListOptions.Simple` | `bool` | 总发送；零值 false | 以精简条目格式列目录。 |
| `ListOptions.Recursive` | `bool` | 总发送；零值 false | 是否递归列出子层级。 |
| `ListOptions.Output` | `string` | 空为 `original` | 目录条目的内容输出形式。 |
| `ListOptions.AbsLimit` | `int` | 0 为 256 | 每个 abstract 的长度上限。 |
| `ListOptions.IncludeAbstract` | `*bool` | nil 省略 | 是否返回 abstract。 |
| `ListOptions.IncludeOverview` | `*bool` | nil 省略 | 是否返回 overview。 |
| `ListOptions.OverviewLimit` | `int` | 0 为 4000 | 每个 overview 的长度上限。 |
| `ListOptions.ShowAllHidden` | `bool` | 总发送；零值 false | 是否显示隐藏节点。 |
| `ListOptions.NodeLimit` | `int` | 0 为 1000 | 可返回节点总数上限。 |
| `ListOptions.Offset` | `int` | 0 省略 | 分页起始偏移。 |
| `ListOptions.Limit` | `int` | 0 省略 | 分页条数上限。 |
| `ListOptions.SortBy` | `string` | 空省略 | 目录排序字段。 |
| `ListOptions.SortOrder` | `string` | 空省略 | 目录排序方向。 |
| `ListOptions.ExtraFields` | `[]string` | nil 省略，空数组发送 | 额外请求返回的条目字段。 |
| `ListOptions.Tags` | `[]string` | nil 省略，空数组发送 | 按业务 tags 筛选目录条目。 |
| `ListOptions.IncludeTags` | `bool` | 仅 true 发送 | 是否在条目中返回 tags。 |
| `TreeOptions.Output` | `string` | 空为 `original` | 树节点内容输出形式。 |
| `TreeOptions.AbsLimit` | `int` | 0 为 128 | 树节点 abstract 长度上限。 |
| `TreeOptions.IncludeAbstract` | `*bool` | nil 省略 | 是否返回节点 abstract。 |
| `TreeOptions.IncludeOverview` | `*bool` | nil 省略 | 是否返回节点 overview。 |
| `TreeOptions.OverviewLimit` | `int` | 0 为 4000 | 节点 overview 长度上限。 |
| `TreeOptions.ShowAllHidden` | `bool` | 总发送；零值 false | 是否显示隐藏节点。 |
| `TreeOptions.DirectoriesOnly` | `bool` | 仅 true 发送 | 是否只返回目录节点。 |
| `TreeOptions.NodeLimit` | `int` | 0 为 1000 | 树节点总数上限。 |
| `TreeOptions.LevelLimit` | `*int` | nil 时为 3 | 树的最大深度。 |
| `TreeOptions.Offset` | `int` | 0 省略 | 树分页偏移。 |
| `TreeOptions.Limit` | `int` | 0 省略 | 树分页条数。 |
| `TreeOptions.ExtraFields` | `[]string` | nil 省略，空数组发送 | 额外请求返回的节点字段。 |
| `TreeOptions.Tags` | `[]string` | nil 省略，空数组发送 | 按业务 tags 筛选树节点。 |
| `TreeOptions.IncludeTags` | `bool` | 仅 true 发送 | 是否返回节点 tags。 |
| `RemoveOptions.Recursive` | `bool` | 总发送；零值 false | 是否递归删除目录。 |
| `RemoveOptions.Wait` | `bool` | 总发送；零值 false | 是否等待删除任务完成。 |
| `RemoveOptions.Timeout` | `*float64` | nil 省略 | 删除等待超时。 |
| `WriteOptions.ACL` | `*ACLSpec` | nil 省略 | 写入时一起设置 ACL。 |
| `WriteOptions.Mode` | `string` | 空为 `replace` | 写入/合并内容的服务端模式。 |
| `WriteOptions.Wait` | `bool` | 总发送；零值 false | 是否等待写入及后处理完成。 |
| `WriteOptions.Timeout` | `*float64` | nil 省略 | 写入等待超时。 |
| `WriteOptions.Telemetry` | `any` | nil 省略 | 写入链路的遥测元数据。 |
| `WriteOptions.ProcessingMode` | `string` | 空省略 | 后处理模式，例如 `vectors_only`。 |
| `WriteOptions.Tags` | `[]string` | nil 省略；非 nil 空数组发送 | 写入后要设置的业务 tags。 |
| `WriteOptions.TagMode` | `string` | 空省略；`clear` 会使 tags 也发送 | 设置、追加或清空 tags 的方式。 |
| `WriteOptions.Extra` | `map[string]any` | nil 省略 | 写入请求的透传扩展参数。 |
| `BatchWriteOperation.URI` | `string` | JSON 必填；相对 `rootURI` 规范化 | 本批次中该文件的目标 URI。 |
| `BatchWriteOperation.Content` | `*string` | nil 省略 | 该文件的 UTF-8 文本内容。 |
| `BatchWriteOperation.ContentBase64` | `*string` | nil 省略 | 该文件的 base64 二进制内容。 |
| `BatchWriteOperation.Mode` | `string` | 空省略 | 单文件写入模式。 |
| `BatchWriteOptions.Wait` | `*bool` | nil 省略；可显式 false | 是否等待整批写入完成。 |
| `BatchWriteOptions.Timeout` | `*float64` | nil 省略 | 批量写入等待超时。 |
| `BatchWriteOptions.Telemetry` | `any` | nil 省略 | 批量写入遥测。 |
| `BatchWriteOptions.Extra` | `map[string]any` | nil 省略 | 批量写入的扩展参数。 |
| `SetTagsOptions.Mode` | `string` | 空为 `replace` | tags 的 replace/append/clear 行为。 |
| `SetTagsOptions.Recursive` | `bool` | 总发送；零值 false | 是否递归给子项设置 tags。 |
| `SetTagsOptions.Telemetry` | `any` | nil 省略 | 设置 tags 的遥测。 |
| `SetTagsOptions.Extra` | `map[string]any` | nil 省略 | 设置 tags 的扩展参数。 |
| `ReindexOptions.Mode` | `string` | nil options 时为 `vectors_only`；非 nil 空省略 | 重建索引的处理模式。 |
| `ReindexOptions.Wait` | `bool` | nil options 时 true；非 nil options 总发送，零值 false | 是否同步等待重建；传 options 时须显式 `true` 才同步。 |
| `ReindexOptions.Force` | `bool` | 仅 true 发送 | 是否强制重建。 |
| `ReindexOptions.Recursive` | `*bool` | nil 时 true | 是否递归重建子树。 |
| `ReindexOptions.Tags` | `[]string` | nil 省略；非 nil 空数组发送 | 重建时更新的业务 tags。 |
| `ReindexOptions.TagMode` | `string` | 空省略；`clear` 会使 tags 也发送 | 重建时处理 tags 的方式。 |
| `ReindexOptions.Extra` | `map[string]any` | nil 省略 | 重建的透传扩展参数。 |

##### 技能、检索与上下文 Options

| 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|
| `AddSkillOptions.Wait` | `bool` | 总发送；零值 false | 是否等待技能导入完成。 |
| `AddSkillOptions.Timeout` | `*float64` | nil 省略 | 技能导入等待超时。 |
| `AddSkillOptions.Telemetry` | `any` | nil 省略 | 技能导入遥测。 |
| `AddSkillOptions.Extra` | `map[string]any` | nil 省略 | 技能导入扩展参数。 |
| `AddSkillOptions.TargetURI` | `any` | nil 省略 | 限定导入到的 skills 根 URI。 |
| `CompileOptions.Instruction` | `string` | 空省略 | 编译资源时给技能的执行指令。 |
| `CompileOptions.Args` | `map[string]any` | 仅 `len>0` 发送 | 编译技能的命名参数。 |
| `CompileOptions.Extra` | `map[string]any` | nil 省略 | 编译透传参数。 |
| `ListSkillsOptions.NodeLimit` | `int` | 0 为 1000 | 技能节点返回上限。 |
| `ListSkillsOptions.TargetURI` | `any` | nil 省略 | 要列举的 skills 根。 |
| `FindSkillsOptions.Limit` | `int` | 0 为 10 | 相似技能结果数上限。 |
| `FindSkillsOptions.ScoreThreshold` | `*float64` | nil 省略 | 技能检索最低分数。 |
| `FindSkillsOptions.Level` | `[]int` | nil/空省略 | 限定技能层级。 |
| `FindSkillsOptions.Telemetry` | `any` | nil 省略 | 技能检索遥测。 |
| `FindSkillsOptions.TargetURI` | `any` | nil 省略 | 技能检索根 URI。 |
| `ValidateSkillOptions.Strict` | `bool` | nil opts 时 false；非 nil 时总发送 | 是否严格校验技能。 |
| `ValidateSkillOptions.SourcePath` | `string` | 空省略 | 供校验结果记录的来源路径。 |
| `ValidateSkillOptions.SkillDirName` | `string` | 空省略 | 认定的技能目录名。 |
| `ValidateSkillOptions.TargetURI` | `any` | nil 省略 | 校验所在的 skills 根。 |
| `GetSkillOptions.IncludeContent` | `*bool` | nil 省略 | 是否带回技能正文。 |
| `GetSkillOptions.IncludeFiles` | `*bool` | nil 时 SDK 默认 true | 是否带回技能文件清单。 |
| `GetSkillOptions.IncludeSource` | `bool` | 总发送；零值 false | 是否带回技能来源信息。 |
| `GetSkillOptions.Level` | `*int` | nil 省略 | 指定读取的技能层级。 |
| `GetSkillOptions.TargetURI` | `any` | nil 省略 | 指定读取的 skills 根。 |
| `UpdateSkillOptions.Wait` | `bool` | 总发送；零值 false | 是否同步等待技能更新。 |
| `UpdateSkillOptions.Timeout` | `*float64` | nil 省略 | 更新等待超时。 |
| `UpdateSkillOptions.SourceMetadata` | `map[string]any` | nil 省略 | 更新技能的来源元数据。 |
| `UpdateSkillOptions.Telemetry` | `any` | nil 省略 | 技能更新遥测。 |
| `UpdateSkillOptions.TargetURI` | `any` | nil 省略 | 被更新技能所在根。 |
| `UpdateSkillOptions.Extra` | `map[string]any` | nil 省略 | 技能更新扩展参数。 |
| `DeleteSkillOptions.TargetURI` | `any` | nil 省略 | 限定删除的 skills 根。 |
| `FindOptions.EventsTimeDecayProtection` | `string` | 空省略 | 控制事件记忆的时间衰减保护策略。 |
| `FindOptions.TargetURI` | `any` | nil 省略；string/`[]string` 规范化 | 限定无 Session 检索的资源根。 |
| `FindOptions.Image` | `string` | 空省略；本地文件转 data URI | 参与多模态检索的图片。 |
| `FindOptions.Limit` | `int` | 0 为 10 | 结果数上限（无 `NodeLimit` 时）。 |
| `FindOptions.NodeLimit` | `*int` | nil 使用 Limit；非 nil 覆盖 Limit | 检索节点数上限，可显式 0。 |
| `FindOptions.ScoreThreshold` | `*float64` | nil 省略 | 最低相关性分数。 |
| `FindOptions.Filter` | `map[string]any` | nil 省略 | 服务端结构化过滤条件。 |
| `FindOptions.ContextType` | `any` | nil 省略 | 限定搜索的上下文类型。 |
| `FindOptions.IncludeProvenance` | `*bool` | nil 省略 | 是否返回来源归因。 |
| `FindOptions.ReadContent` | `*bool` | nil 省略 | 是否读取/返回更完整的命中内容。 |
| `FindOptions.Telemetry` | `any` | nil 省略 | 检索遥测。 |
| `FindOptions.Since` | `string` | 空省略 | 以 `TimeField` 为准的时间下界。 |
| `FindOptions.Until` | `string` | 空省略 | 以 `TimeField` 为准的时间上界。 |
| `FindOptions.TimeField` | `string` | 空省略 | 时间筛选采用的字段。 |
| `FindOptions.Level` | `[]int` | nil 省略 | 限定可搜索的节点层级。 |
| `FindOptions.Tags` | `[]string` | nil 省略 | 按业务 tags 筛选。 |
| `FindOptions.Extra` | `map[string]any` | nil 省略 | 检索扩展参数。 |
| `SearchOptions.EventsTimeDecayProtection` | `string` | 空省略 | 控制 Session 检索中事件记忆的时间衰减保护。 |
| `SearchOptions.SearchType` | `string` | 空省略 | 指定服务端搜索策略/类型。 |
| `SearchOptions.TargetURI` | `any` | nil 省略；string/`[]string` 规范化 | 限定 Session 检索资源根。 |
| `SearchOptions.Image` | `string` | 空省略；本地文件转 data URI | 参与多模态搜索的图片。 |
| `SearchOptions.SessionID` | `string` | 空省略 | 让搜索带入的会话上下文。 |
| `SearchOptions.Limit` | `int` | 0 为 10 | 结果数上限（无 `NodeLimit` 时）。 |
| `SearchOptions.NodeLimit` | `*int` | nil 使用 Limit；非 nil 覆盖 Limit | 搜索节点数上限。 |
| `SearchOptions.ScoreThreshold` | `*float64` | nil 省略 | 最低相关性分数。 |
| `SearchOptions.Filter` | `map[string]any` | nil 省略 | 结构化过滤条件。 |
| `SearchOptions.ContextType` | `any` | nil 省略 | 要搜索的上下文类型。 |
| `SearchOptions.IncludeProvenance` | `*bool` | nil 省略 | 是否附来源归因。 |
| `SearchOptions.ReadContent` | `*bool` | nil 省略 | 是否读取/返回更完整内容。 |
| `SearchOptions.Telemetry` | `any` | nil 省略 | 搜索遥测。 |
| `SearchOptions.Since` | `string` | 空省略 | 时间下界。 |
| `SearchOptions.Until` | `string` | 空省略 | 时间上界。 |
| `SearchOptions.TimeField` | `string` | 空省略 | 时间筛选字段。 |
| `SearchOptions.Level` | `[]int` | nil 省略 | 节点层级筛选。 |
| `SearchOptions.Tags` | `[]string` | nil 省略 | tags 筛选。 |
| `SearchOptions.Extra` | `map[string]any` | nil 省略 | 搜索扩展参数。 |

##### 上下文、Session、任务与系统 Options

| 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|
| `SearchContextOptions.Image` | `string` | 空省略；本地文件转 data URI | 上下文组装时的多模态图片查询。 |
| `SearchContextOptions.SessionID` | `string` | 空省略 | 组装上下文所依据的会话。 |
| `SearchContextOptions.SearchType` | `string` | 空省略 | 服务端搜索策略。 |
| `SearchContextOptions.Limit` | `*int` | nil 省略 | 单类上下文结果数上限。 |
| `SearchContextOptions.NodeLimit` | `*int` | nil 省略 | 搜索节点数上限。 |
| `SearchContextOptions.ScoreThreshold` | `*float64` | nil 省略 | 最低相关性分数。 |
| `SearchContextOptions.Filter` | `map[string]any` | nil 省略 | 结构化过滤条件。 |
| `SearchContextOptions.ContextType` | `any` | nil 省略 | 组装的上下文类型范围。 |
| `SearchContextOptions.IncludeProvenance` | `*bool` | nil 省略 | 是否附带来源归因。 |
| `SearchContextOptions.Tags` | `[]string` | nil 省略 | tags 筛选条件。 |
| `SearchContextOptions.Since` | `string` | 空省略 | 时间下界。 |
| `SearchContextOptions.Until` | `string` | 空省略 | 时间上界。 |
| `SearchContextOptions.TimeField` | `string` | 空省略 | 时间筛选字段。 |
| `SearchContextOptions.EventsTimeDecayProtection` | `string` | 空省略 | 事件记忆的时间衰减保护策略。 |
| `SearchContextOptions.QueryExpansion` | `string` | 空省略 | 服务端查询扩展策略。 |
| `SearchContextOptions.MaxTokens` | `*int` | nil 省略 | 渲染上下文的 token 预算。 |
| `SearchContextOptions.Quotas` | `map[string]int` | nil 省略 | 各类上下文的配额。 |
| `SearchContextOptions.Purpose` | `string` | 空省略 | 告知服务端该上下文的使用目的。 |
| `SearchContextOptions.Detail` | `any` | nil 省略 | 请求的上下文细节级别。 |
| `SearchContextOptions.DedupTurns` | `*int` | nil 省略 | 去重的最近会话轮次数。 |
| `SearchContextOptions.ExcludeURIs` | `[]string` | nil 省略 | 排除的资源 URI。 |
| `SearchContextOptions.PeerScope` | `string` | 空省略 | 限定可检索 peer 的范围。 |
| `SearchContextOptions.OtherPeerPenalty` | `any` | nil 省略 | 对其他 peer 命中的惩罚设置。 |
| `SearchContextOptions.Rewrite` | `any` | nil 省略 | 上下文重写开关或配置。 |
| `SearchContextOptions.RewriteMaxBullets` | `*int` | nil 省略 | 重写摘要的最大 bullet 数。 |
| `SearchContextOptions.Telemetry` | `any` | nil 省略 | 上下文组装遥测。 |
| `SearchContextOptions.Extra` | `map[string]any` | nil 省略 | 上下文组装扩展参数；不能改写固定 `mode=context`。 |
| `GrepOptions.CaseInsensitive` | `bool` | 总发送；零值 false | 正则/文本匹配是否忽略大小写。 |
| `GrepOptions.NodeLimit` | `*int` | nil 省略 | grep 扫描节点数上限。 |
| `GrepOptions.LevelLimit` | `*int` | nil 省略 | grep 扫描深度上限。 |
| `GrepOptions.ExcludeURI` | `string` | 空省略 | 排除的 URI 子树。 |
| `GrepOptions.Tags` | `[]string` | nil 省略 | grep 的 tags 筛选。 |
| `GrepOptions.IncludeTags` | `bool` | 仅 true 发送 | 是否在 grep 结果中返回 tags。 |
| `GlobOptions.NodeLimit` | `*int` | nil 省略 | 路径匹配节点数上限。 |
| `GlobOptions.Tags` | `[]string` | nil 省略 | glob 的 tags 筛选。 |
| `GlobOptions.IncludeTags` | `bool` | 仅 true 发送 | 是否在 glob 结果中返回 tags。 |
| `CreateSessionOptions.SessionID` | `string` | 空省略 | 调用方指定的会话 ID；省略时服务端生成。 |
| `CreateSessionOptions.MemoryPolicy` | `map[string]any` | nil 省略 | 新会话的记忆保留/提取策略。 |
| `CreateSessionOptions.AutoCommitPolicy` | `map[string]any` | nil 省略，除非 DisableAutoCommit | 新会话的自动提交策略。 |
| `CreateSessionOptions.DisableAutoCommit` | `bool` | true 时发 `auto_commit_policy:null` | 显式关闭自动提交并优先于 policy。 |
| `CreateSessionOptions.MemoryExtractionConfig` | `map[string]any` | nil 省略 | 记忆提取器配置。 |
| `CreateSessionOptions.Telemetry` | `any` | nil 省略 | 创建会话遥测。 |
| `CreateSessionOptions.Extra` | `map[string]any` | nil 省略 | 创建会话扩展参数。 |
| `GetSessionOptions.AutoCreate` | `bool` | 仅 true 发送 | 不存在时是否由服务端自动创建会话。 |
| `UpdateSessionConfigOptions.MemoryExtractionConfig` | `map[string]any` | nil 省略 | 更新会话记忆提取配置。 |
| `UpdateSessionConfigOptions.AutoCommitPolicy` | `*map[string]any` | nil 指针省略；`Map(nil)` 发 JSON null | 更新、清空或不改变自动提交策略。 |
| `UpdateSessionConfigOptions.Telemetry` | `any` | nil 省略 | 更新会话配置遥测。 |
| `UpdateSessionConfigOptions.Extra` | `map[string]any` | nil 省略 | 更新会话配置扩展参数。 |
| `AddMessageOptions.Content` | `*string` | Parts 为空时必填；`String(\"\")` 可发空内容 | 单段纯文本消息。 |
| `AddMessageOptions.Parts` | `[]map[string]any` | 非空时优先于 Content | 多段/多模态消息内容。 |
| `AddMessageOptions.CreatedAt` | `string` | 空省略 | 调用方提供的消息创建时间。 |
| `AddMessageOptions.PeerID` | `string` | 空省略 | 消息所属 peer。 |
| `AddMessageOptions.TurnID` | `string` | 空省略 | 消息所属对话轮次。 |
| `AddMessageOptions.MessageKind` | `string` | 空省略 | 消息业务类型。 |
| `AddMessageOptions.SourceMessageIDs` | `[]string` | nil 省略，空数组发送 | 此消息来源的消息 ID。 |
| `AddMessageOptions.Telemetry` | `any` | nil 省略 | 加消息遥测。 |
| `AddMessageOptions.Extra` | `map[string]any` | nil 省略 | 加消息扩展参数。 |
| `Message.Role` | `string` | JSON 必填 | BatchAddMessages 中该消息的角色。 |
| `Message.Content` | `*string` | `omitempty`；nil 省略 | Batch 消息的文本内容。 |
| `Message.Parts` | `[]map[string]any` | `omitempty`；nil/空省略 | Batch 消息的多段内容。 |
| `Message.CreatedAt` | `string` | 空省略 | Batch 消息时间。 |
| `Message.PeerID` | `string` | 空省略 | Batch 消息 peer。 |
| `Message.TurnID` | `string` | 空省略 | Batch 消息轮次。 |
| `Message.MessageKind` | `string` | 空省略 | Batch 消息类型。 |
| `Message.SourceMessageIDs` | `[]string` | `omitempty` | Batch 消息来源 ID。 |
| `Message.Telemetry` | `any` | `omitempty` | Batch 消息遥测。 |
| `BatchAddMessagesOptions.Telemetry` | `any` | nil 省略 | 批量加消息遥测。 |
| `BatchAddMessagesOptions.Extra` | `map[string]any` | nil 省略 | 批量加消息扩展参数。 |
| `CommitSessionOptions.KeepRecentCount` | `*int` | nil 省略 | 提交时保留的最近消息数。 |
| `CommitSessionOptions.RetentionMode` | `string` | 空省略 | 服务端采用的原始消息保留模式。 |
| `CommitSessionOptions.KeepRecentTurnCount` | `*int` | nil 省略 | 提交时保留的最近轮次数。 |
| `CommitSessionOptions.RetainedMessageTokenBudget` | `*int` | nil 省略 | 保留原始消息的 token 预算。 |
| `CommitSessionOptions.MinRawTailSteps` | `*int` | nil 省略 | 至少保留的原始尾部步骤数。 |
| `CommitSessionOptions.Telemetry` | `any` | nil 省略 | 会话提交遥测。 |
| `CommitSessionOptions.EventTags` | `[]string` | nil 不发；非 nil（含空数组）发 | 覆盖本次事件记忆的 tags；空数组显式跳过默认 tags。 |
| `CommitSessionOptions.Extra` | `map[string]any` | nil 省略 | 提交会话扩展参数。 |
| `ListTasksOptions.TaskType` | `string` | 空省略 | 按任务类型过滤。 |
| `ListTasksOptions.Status` | `string` | 空省略 | 按任务状态过滤。 |
| `ListTasksOptions.ResourceID` | `string` | 空省略 | 按相关资源过滤。 |
| `ListTasksOptions.Limit` | `int` | 非正省略 | 返回任务数上限。 |
| `WaitProcessedOptions.Timeout` | `*float64` | nil 省略 | 等待系统处理完既有工作的服务端超时。 |
| `ObserverStatusOptions.Format` | `string` | 空省略；只读首个 options | 系统观察状态的格式。 |

##### OVPack、资产、管理与 Agent Evolution Options

| 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|
| `PackOptions.IncludeVectors` | `bool` | 仅 true 发送 | 导出/备份中是否包含向量数据。 |
| `ImportPackOptions.OnConflict` | `string` | 空省略 | 导入时 URI/资源冲突的处理策略。 |
| `ImportPackOptions.VectorMode` | `string` | 空省略 | 导入时处理包内向量的方式。 |
| `ResolveAssetsOptions.CatalogYAML` | `string` | 空省略 | 补充或覆盖的 Assets catalog YAML。 |
| `ResolveAssetsOptions.ManifestLabel` | `string` | 空省略 | 选择 manifest 标签。 |
| `ResolveAssetsOptions.CatalogLabel` | `string` | 空省略 | 选择 catalog 标签。 |
| `ResolveAssetsOptions.Extra` | `map[string]any` | nil 省略 | Assets 解析的扩展参数。 |
| `AssetGitAuth.Username` | `string` | `omitempty` | 私有 Git 预检使用的用户名。 |
| `AssetGitAuth.Token` | `string` | `omitempty` | 私有 Git 预检使用的 token/password。 |
| `PreflightAssetOptions.Branch` | `string` | 空省略 | 要预检的 Git 分支。 |
| `PreflightAssetOptions.Commit` | `string` | 空省略 | 要预检的确定 commit。 |
| `PreflightAssetOptions.AuthConfig` | `*AssetGitAuth` | nil 省略 | 一次性私有 Git 访问认证。 |
| `PreflightAssetOptions.Extra` | `map[string]any` | nil 省略 | 预检的扩展参数。 |
| `ExperienceTrajectoryOptions.Limit` | `*int` | nil 省略 | 轨迹页大小。 |
| `ExperienceTrajectoryOptions.Offset` | `*int` | nil 省略 | 轨迹分页偏移。 |
| `ExperienceTrajectoryOptions.StartDate` | `string` | 空省略 | 轨迹日期筛选下界。 |
| `ExperienceTrajectoryOptions.EndDate` | `string` | 空省略 | 轨迹日期筛选上界。 |
| `ExperienceOutcomeOptions.StartDate` | `string` | 空省略 | outcome 日期筛选下界。 |
| `ExperienceOutcomeOptions.EndDate` | `string` | 空省略 | outcome 日期筛选上界。 |
| `AdminCreateAccountOptions.UserConfig` | `map[string]any` | nil 省略 | 创建管理员用户时写入的用户配置。 |
| `AdminCreateAccountOptions.Seed` | `*string` | nil 省略；可显式空串 | 生成账户/管理员凭据的种子。 |
| `AdminRegisterUserOptions.UserConfig` | `map[string]any` | nil 省略 | 新用户的初始化配置。 |
| `AdminRegisterUserOptions.Seed` | `*string` | nil 省略；可显式空串 | 生成用户 key 的种子。 |
| `AdminListAccountsOptions.Name` | `string` | 空省略 | 用 `*`/`?` 通配过滤 account ID。 |
| `AdminListAccountsOptions.Limit` | `*int` | nil 时不分页 | 账户页大小；给定后才启用分页。 |
| `AdminListAccountsOptions.Page` | `*int` | Limit 非 nil 且 Page nil 时为 1 | 1-based 账户页码。 |
| `AdminListUsersOptions.Limit` | `*int` | nil 时不分页 | 用户页大小。 |
| `AdminListUsersOptions.Name` | `string` | 空省略 | 用 `*`/`?` 通配过滤 user ID。 |
| `AdminListUsersOptions.Role` | `string` | 空省略 | 按用户角色过滤。 |
| `AdminListUsersOptions.Page` | `*int` | Limit 非 nil 且 Page nil 时为 1 | 1-based 用户页码。 |
| `AdminRegenerateKeyOptions.Seed` | `*string` | nil 时无 body；可显式空串 | 重新生成 key 使用的种子。 |
| `AdminMigrateOptions.Cleanup` | `bool` | false/opts nil 为 `action=migrate` | true 时改为执行迁移数据清理。 |

##### 结构化结果、分页与错误字段

| 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|
| `ListPage.Result` | `[]any` | 服务端未给时零值 nil | 当前目录页的原始条目。 |
| `ListPage.HasMore` | `bool` | 服务端未给时 false | 后续是否仍有目录页。 |
| `TreePage.Result` | `[]map[string]any` | 服务端未给时零值 nil | 当前树页的原始节点。 |
| `TreePage.HasMore` | `bool` | 服务端未给时 false | 后续是否仍有树页。 |
| `FindResult.Memories` | `[]MatchedContext` | 响应缺失时 nil | 命中的记忆上下文。 |
| `FindResult.Resources` | `[]MatchedContext` | 响应缺失时 nil | 命中的资源上下文。 |
| `FindResult.Skills` | `[]MatchedContext` | 响应缺失时 nil | 命中的技能上下文。 |
| `FindResult.QueryPlan` | `*QueryPlan` | 响应缺失时 nil | 服务端生成的查询扩展计划。 |
| `FindResult.QueryResults` | `[]map[string]any` | 响应缺失时 nil | 原样的附加查询结果，内部键不承诺。 |
| `FindResult.Total` | `int` | 响应缺失为 0 | 服务端报告的总命中数。 |
| `MatchedContext.URI` | `string` | 响应缺失为空 | 命中节点 URI。 |
| `MatchedContext.ContextType` | `string` | 响应缺失为空 | 命中的上下文类别。 |
| `MatchedContext.Level` | `int` | 响应缺失为 0 | 命中节点层级。 |
| `MatchedContext.Abstract` | `string` | 响应缺失为空 | 命中节点摘要。 |
| `MatchedContext.Content` | `string` | 响应缺失为空 | 命中节点内容。 |
| `MatchedContext.Overview` | `string` | 响应缺失为空 | 命中节点概览。 |
| `MatchedContext.Category` | `string` | 响应缺失为空 | 命中所属业务类别。 |
| `MatchedContext.OriginScore` | `*float64` | 未返回为 nil | 检索管线的原始相关性分。 |
| `MatchedContext.TimeScore` | `*float64` | 未返回为 nil | 时间衰减/时间相关性分。 |
| `MatchedContext.Score` | `float64` | 响应缺失为 0 | 最终排序分。 |
| `MatchedContext.MatchReason` | `string` | 响应缺失为空 | 服务端给出的命中原因。 |
| `MatchedContext.Tags` | `[]string` | 响应缺失为 nil | 命中节点的 `tags`。 |
| `SearchContextResult.Entries` | `[]SearchContextEntry` | 响应缺失为 nil | 可注入的上下文条目。 |
| `SearchContextResult.Rendered` | `string` | 响应缺失为空 | 已渲染的上下文文本。 |
| `SearchContextResult.Digest` | `string` | 响应缺失为空 | 上下文摘要。 |
| `SearchContextResult.Stats` | `map[string]any` | 响应缺失为 nil | 服务端统计；内部键不承诺。 |
| `SearchContextEntry.URI` | `string` | 响应缺失为空 | 上下文来源 URI。 |
| `SearchContextEntry.Category` | `string` | 响应缺失为空 | 上下文类别。 |
| `SearchContextEntry.Score` | `float64` | 响应缺失为 0 | 条目相关性分数。 |
| `SearchContextEntry.Detail` | `string` | 响应缺失为空 | 服务端给出的条目细节级别。 |
| `SearchContextEntry.Text` | `string` | 响应缺失为空 | 可注入的文本。 |
| `SearchContextEntry.Origin` | `string` | 响应缺失为空 | 条目的来源说明。 |
| `QueryPlan.Queries` | `[]TypedQuery` | 响应缺失为 nil | 按上下文类型拆分的子查询。 |
| `QueryPlan.Raw` | `map[string]any` | JSON 不编码 | 未建模 query plan 信息的原始保存位置。 |
| `TypedQuery.Query` | `string` | 响应缺失为空 | 生成的子查询文本。 |
| `TypedQuery.ContextType` | `string` | 响应缺失为空 | 子查询目标上下文类型。 |
| `TypedQuery.Intent` | `string` | 响应缺失为空 | 服务端识别出的查询意图。 |
| `TypedQuery.Priority` | `int` | 响应缺失为 0 | 子查询执行优先级。 |
| `TypedQuery.TargetDirectories` | `[]string` | 响应缺失为 nil | 子查询优先搜索的目录。 |
| `Error.Code` | `string` | 空时 `Error()` 只返回 Message | API 业务错误码，供 `IsCode` 判断。 |
| `Error.Message` | `string` | 空字符串允许 | 人可读错误消息。 |
| `Error.Details` | `map[string]any` | 未给时 nil | API 错误细节，内部键不承诺。 |
| `Error.StatusCode` | `int` | 未给时 0 | HTTP 状态码。 |
| `ErrorInfo.Code` | `string` | 空会归一为 `UNKNOWN` | 响应 envelope 中的原始错误码。 |
| `ErrorInfo.Message` | `string` | 空会归一为 `Unknown error` | 响应 envelope 中的原始错误消息。 |
| `ErrorInfo.Details` | `map[string]any` | 未给时 nil | 响应 envelope 中的错误细节。 |

## 兼容性、升级与测试

Go SDK 是独立 module，与服务端共同演进。升级时请查看 release notes，重点复核异步任务默认行为、Options 的省略/显式零值语义，以及服务端版本是否支持目标能力。不要将旧 `agent_id` 配置迁入 Go SDK；改用 `ActorPeerID`。

本地验证与可运行 smoke 示例：

```bash
cd sdk/go
go test ./...
go run ./examples/basic_usage
```

运行示例前，在 `examples/basic_usage/main.go` 顶部填入测试服务的 `baseURL` 和 `apiKey`。示例会覆盖资源、技能、Watch、Session、记忆提取和检索流程。
