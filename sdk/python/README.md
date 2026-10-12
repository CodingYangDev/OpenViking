# OpenViking Python SDK

`openviking-sdk` 是面向已有 OpenViking Server 的轻量级 Python HTTP 客户端。本 README 是 Python SDK 唯一的中文使用文档，覆盖同步/异步调用、常用工作流、错误处理和公开能力速查。

## SDK 定位与支持范围

当应用只需通过 HTTP 调用 OpenViking、希望避免引入本地运行时、服务端和 CLI 依赖时，使用 `openviking-sdk`。需要运行 Server、本地运行时或使用主包兼容导入时，使用 `openviking` 主包。

SDK 覆盖资源和技能、文件系统、检索、Session/Memory、Tags、任务、Watch、OVPack、ACL、状态观察、资产和管理员接口。服务端 HTTP 契约及每个字段的完整解释可参考仓库 API 文档。

## 安装

```bash
pip install --upgrade openviking-sdk
```

要求 Python 3.8+ 和可访问的 OpenViking HTTP 服务，例如 `http://127.0.0.1:1933`。

- PyPI 包名：`openviking-sdk`
- Python 导入名：`openviking_sdk`

```python
from openviking_sdk import AsyncHTTPClient, SyncHTTPClient
```

## 初始化、认证与配置

配置优先级从高到低为：显式构造参数、环境变量、`ovcli.conf`。支持的环境变量包括 `OPENVIKING_URL`、`OPENVIKING_API_KEY`、`OPENVIKING_ACCOUNT`、`OPENVIKING_USER`、`OPENVIKING_ACTOR_PEER_ID` 和 `OPENVIKING_TIMEOUT`。`ovcli.conf` 可由 `OPENVIKING_CLI_CONFIG_FILE` 指定，默认位于 `~/.openviking/ovcli.conf`。

```python
from openviking_sdk import SyncHTTPClient

client = SyncHTTPClient(
    url="http://127.0.0.1:1933",
    api_key="your-user-key",
)
client.initialize()
try:
    print(client.health())
finally:
    client.close()
```

普通部署通常只需 `api_key`。使用 root key 并代入指定租户时，增加 `account` 与 `user`。`user_id` 是 `user` 的兼容别名；`agent_id` 是 `actor_peer_id` 的兼容别名，但 `actor_peer_id` 与 `agent_id` 不能同时传入。

异步客户端使用相同配置，并以 `await client.initialize()` 和 `await client.close()` 管理生命周期。

## 五分钟跑通：导入资源、等待任务并检索

资源导入默认异步。应查询本次返回的 `task_id`，在任务 `completed` 后再检索；`failed` 和 `cancelled` 必须作为失败处理。

```python
import time

from openviking_sdk import SyncHTTPClient

client = SyncHTTPClient(url="http://127.0.0.1:1933", api_key="your-user-key")
client.initialize()
try:
    created = client.add_resource(
        path="./docs/guide.md",
        to="viking://resources/demo-guide",
        options={"reason": "导入产品文档"},
    )
    task_id = created["task_id"]
    while True:
        task = client.get_task(task_id)
        status = task["status"]
        if status == "completed":
            break
        if status in {"failed", "cancelled"}:
            raise RuntimeError(f"resource task {task_id} ended with {status}")
        time.sleep(0.5)

    result = client.find(query="产品文档", target_uri="viking://resources/demo-guide", limit=5)
    print(result)
finally:
    client.close()
```

异步程序将同一调用替换为 `await client.add_resource(...)`、`await client.get_task(...)` 和 `await client.find(...)`。请勿在 async 程序中用同步 client 阻塞事件循环。

如果只需写入和向量化，不生成或刷新 `.abstract.md` / `.overview.md`，使用 `options={"processing_mode": "vectors_only"}`。

## 核心能力

### 资源、技能与 Watch

`add_resource` 自动处理本地文件上传；可导入文件、目录和 URL。`add_skill` 导入技能，二者均可能产生后台任务。`list_skills`、`find_skills`、`validate_skill`、`get_skill`、`update_skill`、`delete_skill` 用于技能管理。

资源导入时设置 watch 配置后会创建 Watch；使用 `list_watches`、`get_watch`、`update_watch`、`trigger_watch` 和 `delete_watch` 管理它。所有可能异步完成的操作都应按上节方式轮询其任务。

### 文件系统、内容、Tags 与 ACL

常用文件操作为 `ls`/`ls_page`、`tree`/`tree_page`、`stat`、`attrs`、`mkdir`、`read`、`download_bytes`、`write`、`batch_write`、`mv`、`rm`、`abstract`、`overview` 和 `reindex`。需要分页元数据时使用 `ls_page` 或 `tree_page`，而不是只读取数组结果。

```python
client.mkdir(uri="viking://resources/demo-dir")
page = client.ls_page(uri="viking://resources", options={"limit": 50})
client.write(
    uri="viking://resources/demo-dir/note.md",
    content="# 笔记",
    options={"tags": ["team=search", "source=python"], "tag_mode": "replace"},
)
```

`set_tags` 的模式是 `replace`、`append` 和 `clear`；Tags 是调用方定义的业务元数据。ACL 使用 `acl_get`、`acl_set`、`acl_grant`、`acl_revoke` 和 `acl_delete`。

### 检索

`find` 用于无 Session 上下文的检索，`search` 用于带可选 Session 上下文的检索，`search_context` 生成可注入 Agent 的上下文；`grep` 与 `glob` 处理文本与路径匹配。

图片检索通过 `find` 或 `search` 的 `image` 显式参数完成，接受本地路径、bytes、data URI、HTTP URL 或 `viking://` URI。服务端需要配置多模态 embedding 模型。

```python
result = client.search(
    query="红色海报风格",
    target_uri="viking://resources/images",
    image="./poster.png",
    limit=5,
)
```

高频字段用显式参数传递；进阶字段使用有类型提示的 `options` 字典。尚未在当前 SDK 正式建模的新服务端字段，才通过 `options={"extra": {...}}` 临时传递；不要用 `extra` 覆盖 SDK 已定义字段。

### Session、Memory 与后台任务

使用 `create_session`、`get_session`、`update_session_config`、`session_exists`、`delete_session` 管理 Session。`client.session(session_id)` 提供 `add_message`、`batch_add_messages`、`get_session_context`、`get_session_archive` 和 `commit`。

```python
session = client.create_session(
    session_id="demo-session",
    options={"memory_extraction_config": {"events": {"tags": ["team=search"]}}},
)
client.session(session_id="demo-session").add_message(
    role="user",
    content="请记住我偏好简洁回答",
)
client.session(session_id="demo-session").commit(
    options={"event_tags": ["team=search"]},
)
```

创建或更新 Session 时，显式 `None` 与省略字段语义不同：例如 `auto_commit_policy=None` 会显式禁用自动 commit，省略则保持服务端默认或原值。commit 时 `event_tags=[]` 表示本次显式跳过 Session 默认 Tags。

`get_task`、`list_tasks` 和 `cancel_task` 精确管理任务。不要用全局 `wait_processed()` 替代等待某一请求创建的任务。

### OVPack、状态、资产与管理员接口

- OVPack：`export_ovpack`、`backup_ovpack` 将流写入本地文件；`import_ovpack`、`restore_ovpack` 执行导入。
- 状态：`health`、`is_healthy`、`get_status`、`queue_status`、`vikingdb_status`、`models_status`、`check_consistency`。
- 资产：`resolve_openviking_assets`、`preflight_openviking_asset`。
- root key 管理：账户、用户、组、Account Settings、Agent Evolution 和迁移方法均以 `admin_` 前缀暴露。

## 错误处理、超时、取消与易错点

SDK 将服务端错误码映射为 `OpenVikingError` 的具体异常类型。异步调用应捕获同一异常并确保在 `finally` 中关闭 client。

```python
from openviking_sdk import OpenVikingError

try:
    content = client.read(uri="viking://resources/missing.md")
except OpenVikingError as exc:
    print(type(exc).__name__, exc)
```

- 在构造 client 时用 `timeout` 配置请求超时；async 调用还可由上层 task 取消。
- `None` 是显式传值，和不出现在 `options` 字典中的“省略”不同；更新配置时必须按服务端契约选择。
- 后台任务的结果不能通过提交成功推断，必须检查任务状态。
- `reindex` 的默认值与其他 SDK 不同：Python 默认 `wait=True`、`recursive=True`。调整 tags 或模式时不要无意改变这些显式参数。

## API 速查表

| 场景 | 主要方法 |
|---|---|
| 资源、技能、Watch | `add_resource`, `add_skill`, `list_skills`, `find_skills`, `validate_skill`, `get_skill`, `update_skill`, `delete_skill`, `list_watches`, `get_watch`, `update_watch`, `trigger_watch`, `delete_watch` |
| 文件与权限 | `ls`, `ls_page`, `tree`, `tree_page`, `stat`, `attrs`, `mkdir`, `rm`, `mv`, `read`, `download_bytes`, `abstract`, `overview`, `write`, `batch_write`, `set_tags`, `reindex`, `acl_get`, `acl_set`, `acl_grant`, `acl_revoke`, `acl_delete` |
| 检索 | `find`, `search`, `search_context`, `grep`, `glob` |
| Session 与任务 | `create_session`, `list_sessions`, `get_session`, `update_session_config`, `session_exists`, `delete_session`, `get_task`, `list_tasks`, `cancel_task`, `wait_processed`，以及 `client.session(...)` 上的消息与 commit 方法 |
| 数据包与系统 | `export_ovpack`, `backup_ovpack`, `import_ovpack`, `restore_ovpack`, `health`, `is_healthy`, `check_consistency`, observer 状态方法 |
| 编译与快照 | `compile`, `git_commit`, `git_restore`, `git_show`, `git_log`, `git_diff`, `git_get_ignore`, `git_set_ignore`, `git_delete_ignore` |
| 管理与演进 | `admin_create_account`、用户/组管理、Account Settings、Agent Evolution、Experience 与资产方法 |

完整方法签名和 `TypedDict` options 定义位于 `openviking_sdk` 包的类型注释中；IDE 类型提示是调用时的首选参考。

## 兼容性、升级与开发

SDK 支持 Python 3.8+，其发布版本独立于 OpenViking Server 主包。升级时请阅读 release notes，重点复核后台任务默认值、`None`/省略字段语义和目标服务端版本是否支持所用能力。旧 `user_id`、`agent_id` 参数当前仍为兼容别名，新代码使用 `user`、`actor_peer_id`。

从源码开发和验证：

```bash
cd sdk/python
pip install -e .
uv run --active pytest
```

SDK 版本 tag 使用 `python-sdk@<version>`，例如 `python-sdk@0.1.3`；它与主包的 `v<version>` tag 命名空间独立。
