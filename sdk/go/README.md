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

| 场景 | 主要方法 |
|---|---|
| 资源、技能、Watch | `AddResource`, `AddSkill`, `ListSkills`, `FindSkills`, `ValidateSkill`, `GetSkill`, `UpdateSkill`, `DeleteSkill`, `ListWatches`, `GetWatch`, `UpdateWatch`, `DeleteWatch`, `TriggerWatch` |
| 文件与权限 | `List`, `ListPage`, `Tree`, `TreePage`, `Stat`, `Attrs`, `Mkdir`, `Remove`, `Move`, `Read`, `DownloadBytes`, `Abstract`, `Overview`, `Write`, `BatchWrite`, `SetTags`, `Reindex`, `ACL`, `SetACL`, `SetACLMode`, `GrantACL`, `RevokeACL`, `DeleteACL` |
| 检索 | `Find`, `Search`, `SearchContext`, `Grep`, `Glob` |
| Session 与任务 | `CreateSession`, `ListSessions`, `GetSession`, `UpdateSessionConfig`, `SessionExists`, `GetSessionContext`, `GetSessionArchive`, `DeleteSession`, `AddMessage`, `BatchAddMessages`, `CommitSession`, `GetTask`, `CancelTask`, `ListTasks` |
| 数据包与系统 | `ExportOVPack`, `BackupOVPack`, `ImportOVPack`, `RestoreOVPack`, `Health`, `IsHealthy`, `WaitProcessed`, `CheckConsistency`, `GetStatus`, `QueueStatus`, `VikingDBStatus`, `ModelsStatus` |
| 编译 | `Compile` |
| 管理与演进 | `AdminCreateAccount`, `AdminListAccounts`, 用户/组管理方法、`AdminGetAccountSettings`, `AdminGetAgentEvolution`, `AdminSetAgentEvolution`, `ListExperienceTrajectories`, `GetExperienceOutcomes` |

精确的 Options 字段、返回结构和类型说明以 Go 源码的导出注释及 `go doc github.com/volcengine/OpenViking/sdk/go` 为准。

## 兼容性、升级与测试

Go SDK 是独立 module，与服务端共同演进。升级时请查看 release notes，重点复核异步任务默认行为、Options 的省略/显式零值语义，以及服务端版本是否支持目标能力。不要将旧 `agent_id` 配置迁入 Go SDK；改用 `ActorPeerID`。

本地验证与可运行 smoke 示例：

```bash
cd sdk/go
go test ./...
go run ./examples/basic_usage
```

运行示例前，在 `examples/basic_usage/main.go` 顶部填入测试服务的 `baseURL` 和 `apiKey`。示例会覆盖资源、技能、Watch、Session、记忆提取和检索流程。
