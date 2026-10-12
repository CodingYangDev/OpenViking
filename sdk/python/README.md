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
page = client.ls_page(uri="viking://resources", limit=50)
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

以下表格以 `SyncHTTPClient`、`SyncSession`、`SyncHTTPSnapshotNamespace` 和 `options.py` 为准。`AsyncHTTPClient` 的网络调用须 `await`（`session(...)` 本身是同步返回 wrapper 的工厂）；Session wrapper 的两个例外见表中说明。除表中明确写出的 `ListPage`、`TreePage`、标量或列表外，`Dict[str, Any]` 都表示**服务端 `result` 原样映射，不承诺任何键**；服务端升级可增删其中的业务字段。`SearchContextResult` 是已建模的 `TypedDict`，但其字段均可缺失。

### 方法契约

除下列例外外，可选参数为 `None` 时不会写入请求；不传 `options` 或在 `options` 中不放某个键也是省略。所有列出的普通默认值都会传给服务端。`CreateSessionOptions` / `UpdateSessionConfigOptions` 中**键存在**的 `auto_commit_policy` 是例外：值为 `None` 会显式发送 JSON `null`，从而禁用该策略；键缺失才是省略。`wait_processed(timeout=None)` 也是例外：请求体始终含 `{"timeout": timeout}`，故 `None` 会发送 JSON `null`；其 HTTP 超时按 `timeout if timeout else 600.0` 计算，因此 `None` 和 `0` 都使用 600 秒（但请求体分别是 `null`、`0`）。`options.extra` 是受控的扩展字段，不能覆盖 SDK 已固定的参数。

| 分组与方法 | 参数名与默认/省略语义 | 返回类型 / 稳定关键字段 |
|---|---|---|
| 生命周期：`initialize()`、`close()` | 无参数；分别初始化、关闭 HTTP client。 | `None` |
| Session wrapper：`session(session_id=None, must_exist=False)`、`session_exists(session_id)` | `session_id=None` 返回空 ID wrapper；仅 `SyncHTTPClient.session(..., must_exist=True)` 会先读取 Session 并可失败。`AsyncHTTPClient.session` 接受同一参数但当前不执行该检查。 | `SyncSession` / `bool` |
| 资源：`add_resource(path, to=None, parent=None, wait=False, timeout=None, options=None)` | `path` 必填；`to`、`parent`、`timeout` 省略；`wait=False`。`options: AddResourceOptions`。 | `Dict[str, Any]`（服务端 result 原样映射） |
| 技能：`add_skill(data, wait=False, timeout=None, options=None)`；`update_skill(skill_name, data, wait=False, timeout=None, options=None)` | `data` 可为数据、文件或目录；`wait=False`；`timeout=None` 省略。options 分别为 `AddSkillOptions`、`UpdateSkillOptions`。 | `Dict[str, Any]`（原样映射） |
| 技能：`list_skills(node_limit=1000, target_uri=None)`；`find_skills(query, limit=10, score_threshold=None, level=None, telemetry=False, target_uri=None)` | `target_uri`、阈值和 `level` 均可省略；其余使用表内默认值。 | `Dict[str, Any]`（原样映射） |
| 技能：`validate_skill(data, strict=False, source_path=None, skill_dir_name=None, target_uri=None)`；`get_skill(skill_name, include_content=None, include_files=True, include_source=False, level=None, target_uri=None, include_integrity=False)`；`delete_skill(skill_name, target_uri=None)` | `strict=False`；`include_content=None` 真正省略；其余布尔值按默认值发送。 | `Dict[str, Any]`（原样映射） |
| Watch：`list_watches(active_only=False, to_uri=None)`；`get_watch(task_id, to_uri=None)` | `active_only=False`；`to_uri=None` 省略。 | `Dict[str, Any]`（原样映射） |
| Watch：`update_watch(task_id=None, *, to_uri=None, watch_interval=None, is_active=None, reason=None, instruction=None)`；`delete_watch(task_id=None, *, to_uri=None)`；`trigger_watch(task_id=None, *, to_uri=None)` | 三者都要求 `task_id` 或 `to_uri` 至少一个；更新时所有 `None` 字段省略。 | `Dict[str, Any]`（原样映射） |
| 等待：`wait_processed(timeout=None)` | 请求体始终包含 `timeout`：`None` 发送 JSON `null`；`timeout=0` 发送 `0`。HTTP 超时使用 `timeout if timeout else 600.0`，所以二者均为 600 秒；它不是某个任务的完成证明。 | `Dict[str, Any]`（原样映射） |
| 文件列表：`ls(uri, simple=False, recursive=False, output="original", abs_limit=256, show_all_hidden=False, node_limit=1000, sort_by=None, sort_order="asc", extra_fields=None, tags=None, include_tags=False, offset=0, limit=None, include_abstract=None, include_overview=None, overview_limit=4000)` | 分页与筛选参数都是**直接命名参数**；`sort_by`、列表字段、`limit`、`include_*` 为 `None` 时省略。`ls` 只取分页结果的 `result`。 | `List[Any]`；元素是服务端节点原样映射 |
| 文件分页：`ls_page(...)` | 签名与 `ls` 完全相同；尤其是 `limit` 为直接参数，例如 `ls_page(uri, limit=50)`，**不是** `options={"limit": 50}`。 | `ListPage`：稳定键 `result`、`has_more` |
| 树：`tree(uri, output="original", abs_limit=128, show_all_hidden=False, node_limit=1000, level_limit=3, extra_fields=None, tags=None, include_tags=False, offset=0, limit=None, include_abstract=None, include_overview=None, overview_limit=4000, directories_only=False)` | 与 `ls` 相同的直接分页/筛选语义，另有 `level_limit=3`、`directories_only=False`。 | `List[Dict[str, Any]]`；元素原样映射 |
| 树分页：`tree_page(...)` | 签名与 `tree` 完全相同；`limit=None` 省略。 | `TreePage`：稳定键 `result`、`has_more` |
| 文件元数据：`stat(uri)`、`attrs(uri)`；目录：`mkdir(uri, description=None, *, acl=None)` | `description`、`acl` 可省略。 | 前二者 `Dict[str, Any]`（原样映射）；`mkdir` 为 `None` |
| 文件变更：`rm(uri, recursive=False, wait=False, timeout=None)`；`mv(from_uri, to_uri)` | 真实名称是 `rm`、`mv`（没有 `remove`、`move`）；`timeout=None` 省略。 | `None` |
| 内容读取：`read(uri, offset=0, limit=-1)`；`read_raw(uri, offset=0, limit=-1)`；`download_bytes(uri)`；`abstract(uri)`；`overview(uri)` | `offset=0`；`limit=-1` 为 SDK 默认。 | 依次为 `str`、`str`、`bytes`、`str`、`str` |
| 内容写入：`write(uri, content, mode="replace", wait=False, timeout=None, options=None)`；`batch_write(root_uri, operations, wait=True, timeout=None, options=None)` | `mode="replace"`；单写 `wait=False`，批量写 `wait=True`；`timeout=None` 省略。options 分别为 `WriteOptions`、`BatchWriteOptions`。 | `Dict[str, Any]`（原样映射） |
| Tags：`set_tags(uri, tags=None, mode="replace", recursive=False, options=None)` | `tags=None` 不写入；`mode` 可为 `replace`、`append`、`clear`。 | `Dict[str, Any]`（原样映射） |
| ACL：`acl_get(uri)`；`acl_set(uri, entries=None, *, acl_mode=None)`；`acl_grant(uri, principal, level)`；`acl_revoke(uri, principal)`；`acl_delete(uri)` | `acl_set` 至少需 `entries` 或 `acl_mode`；`acl_mode` 仅 `inherit` / `restricted`。 | `Dict[str, Any]`（原样映射） |
| 检索：`find(query="", target_uri="", limit=10, image=None, options=None)` | `target_uri` 可为 `str` 或 `List[str]`；`image=None` 省略，非空值写入 options。`options: FindOptions`。 | `Dict[str, Any]`（原样映射） |
| 检索：`search(query="", session_id=None, target_uri="", limit=10, image=None, options=None)` | 比 `find` 多 `session_id=None`（省略）；`options: SearchOptions`。 | `Dict[str, Any]`（原样映射） |
| 检索上下文：`search_context(query="", session_id=None, target_uri="", limit=10, image=None, options=None)` | 以 context 模式调用；`options: SearchContextOptions`。 | `SearchContextResult`：已建模的可选字段 `entries`、`rendered`、`digest`、`stats`（均可缺失） |
| 路径/文本检索：`grep(uri, pattern, case_insensitive=False, node_limit=256, exclude_uri=None, tags=None, include_tags=False, level_limit=None)`；`glob(pattern, uri="viking://", node_limit=256, extra_fields=None, tags=None, include_tags=False)` | 所有 `None` 的可选筛选参数省略；布尔和数值使用表内默认值。 | `Dict[str, Any]`（原样映射） |
| Session：`create_session(session_id=None, options=None)`；`list_sessions()`；`get_session(session_id, *, auto_create=False)`；`update_session_config(session_id, options=None)`；`delete_session(session_id)` | `session_id=None` 由服务端处理；`auto_create=False`；options 为 `CreateSessionOptions` / `UpdateSessionConfigOptions`，其中 `auto_commit_policy` 的 `None` 特例见本节首段。 | 创建/读取/更新：`Dict[str, Any]`（原样映射）；列表：`List[Any]`（元素原样映射）；删除：`None` |
| Session 内容：`get_session_context(session_id, token_budget=128_000)`；`get_session_archive(session_id, archive_id)` | `token_budget=128_000`。 | `Dict[str, Any]`（原样映射） |
| Session 写入：`add_message(session_id, role, content=None, parts=None, options=None, peer_id=None)`；`batch_add_messages(session_id, messages, options=None)`；`commit_session(session_id, keep_recent_count=0, options=None)` | `content` 与 `parts` 均可省略；`peer_id` 不能同时出现在参数和 options；`options` 分别为 `AddMessageOptions`、`BatchAddMessagesOptions`、`CommitSessionOptions`。 | `Dict[str, Any]`（原样映射） |
| 任务与编译：`get_task(task_id)`；`list_tasks(task_type=None, status=None, resource_id=None, limit=50)`；`cancel_task(task_id)`；`compile(from_uris, to, skill, options=None)` | 列表筛选的 `None` 省略；`get_task` 对 HTTP 404 返回 `None`；`options: CompileOptions`。 | get：`Optional[Dict[str, Any]]`；列表：`List[Dict[str, Any]]`；其余：`Dict[str, Any]`（均为原样映射） |
| 数据包与一致性：`export_ovpack(uri, to, include_vectors=False)`；`backup_ovpack(to, include_vectors=False)`；`import_ovpack(file_path, parent, on_conflict=None, vector_mode=None)`；`restore_ovpack(file_path, on_conflict=None, vector_mode=None)`；`check_consistency(uri)` | `include_vectors=False`；冲突和向量模式为 `None` 时省略。 | `export_ovpack` / `backup_ovpack`：`str` 本地输出路径；`import_ovpack` / `restore_ovpack`：`str` 服务端返回的 URI；一致性检查：`Dict[str, Any]`（原样映射） |
| 重建与健康：`reindex(uri, mode="vectors_only", wait=True, recursive=True, force=False, options=None)`；`health()`；`is_healthy()` | Python 的真实默认值为 `mode="vectors_only"`、`wait=True`、`recursive=True`；`force=False` 时不写入。 | 重建：`Dict[str, Any]`（原样映射）；健康：`bool` |
| 状态：`queue_status(format=None)`、`vikingdb_status(format=None)`、`models_status(format=None)`、`get_status(format=None)`；`observer` | `format` 为 `None` 或 `"table"` / `"json"`；`observer.queue`、`.vikingdb`、`.models`、`.system` 为对应无参状态，`.is_healthy()` 返回布尔值。 | 状态为 `Dict[str, Any]`（原样映射）；observer 健康为 `bool` |
| 管理账户：`admin_create_account(account_id, admin_user_id, user_config=None, seed=None)`；`admin_list_accounts(name=None, limit=None, page=1, query=None)`；`admin_delete_account(account_id)` | `user_config`、`seed`、筛选字段为 `None` 时省略；`page=1`。root key 所需。 | 创建/删除：`Dict[str, Any]`（原样映射）；列表：`List[Any]`（元素原样映射） |
| 管理用户：`admin_register_user(account_id, user_id, role="user", user_config=None, seed=None)`；`admin_list_users(account_id, limit=None, name=None, role=None, page=1, query=None)`；`admin_remove_user(account_id, user_id)`；`admin_set_role(account_id, user_id, role)`；`admin_regenerate_key(account_id, user_id, seed=None)` | `role="user"`；所有 Optional 字段省略规则同上。 | list 为 `List[Any]`；其他为 `Dict[str, Any]`（原样映射） |
| 管理组与迁移：`admin_create_group(account_id, group_id)`；`admin_list_groups(account_id)`；`admin_delete_group(account_id, group_id)`；`admin_list_group_members(account_id, group_id)`；`admin_add_group_member(account_id, group_id, user_id)`；`admin_remove_group_member(account_id, group_id, user_id)`；`admin_migrate(cleanup=False)` | `cleanup=False`。 | `admin_list_groups` 为 `List[Any]`（元素原样映射）；`admin_list_group_members` 及其余方法为 `Dict[str, Any]`（原样映射） |
| Account Settings / 演进：`admin_get_agent_evolution()`；`admin_set_agent_evolution(enabled)`；`admin_get_account_settings(account_id)`；`admin_set_account_agent_evolution(account_id, enabled)` | `enabled` 必填。root key 所需。 | `Dict[str, Any]`（原样映射） |
| Experience 与资产：`list_experience_trajectories(experience_uri, options=None)`；`get_experience_outcomes(experience_uri, options=None)`；`resolve_openviking_assets(manifest_yaml, options=None)`；`preflight_openviking_asset(name, repo_url, options=None)` | options 分别为 `ExperienceTrajectoryOptions`、`ExperienceOutcomeOptions`、`ResolveAssetsOptions`、`PreflightAssetOptions`。 | `Dict[str, Any]`（原样映射） |
| 快照 namespace：`snapshot.commit(*, message, paths=None, branch="main", author_name=None, author_email=None)`；`snapshot.restore(*, project_dir=None, source_commit, branch="main", dry_run=False, message=None, author_name=None, author_email=None)` | 所有 Optional 字段省略；默认分支 `main`，`dry_run=False`。 | `Dict[str, Any]`（原样映射） |
| 快照 namespace：`snapshot.show(target_ref, *, path=None)`；`snapshot.log(*, branch="main", limit=20, paths=None)`；`snapshot.diff(path, *, to_ref, from_ref=None)` | `show(path=None)` 返回提交元数据；有 path 时二进制分支稳定返回 `oid`、`size`、`bytes`。`log` 默认 `main` / `20`。 | show：`Any`；log：`List[Dict[str, Any]]`；diff：`Dict[str, Any]`（其他均原样映射） |
| 快照 namespace：`snapshot.get_gitignore()`；`snapshot.set_gitignore(*, content)`；`snapshot.delete_gitignore()` | 无默认参数；`content` 必填。同步 client 也经 `__getattr__` 支持同签名的 `git_commit`、`git_restore`、`git_show`、`git_log`、`git_diff`、`git_get_ignore`、`git_set_ignore`、`git_delete_ignore`。 | `str` / `None` / `None` |
| Session wrappers：`SyncSession.add_message(role, content=None, parts=None, options=None, peer_id=None)`；`batch_add_messages(messages)`；`commit(keep_recent_count=0, options=None)`；`commit_async(keep_recent_count=0, options=None)` | 均固定使用 wrapper 的 `session_id`；参数语义与 client 同名 Session 写入方法一致。`commit_async` **仅 `SyncSession` 提供**，仍同步执行，是 `commit` 的别名；异步 `Session` 只有 `await commit(...)`，没有 `commit_async`。 | `Dict[str, Any]`（原样映射） |
| `SyncSession`：`delete()`；`load()`；`get_session_context(token_budget=128_000)`；`get_archive(archive_id)` | 无额外 Session ID；`token_budget=128_000`。 | delete：`None`；其余 `Dict[str, Any]`（原样映射） |

### 字段契约

`TypedDict(..., total=False)` 的全部键都可省略。下面的类型名称可直接从 `openviking_sdk` 导入；`Dict[str, Any]`、`Any` 和 `extra` 的内部结构不由 Python SDK 固定。

| 形状 / 使用位置 | 字段契约 |
|---|---|
| `FindOptions` / `SearchOptions` | `FindOptions`：`image`、`node_limit`、`score_threshold`、`filter`、`context_type`、`include_provenance`、`tags`、`since`、`until`、`time_field`（`updated_at` / `created_at`）、`level`、`read_content`、`telemetry`、`events_time_decay_protection`、`extra`。`SearchOptions` 另有 `search_type`（`semantic` / `keywords`）。 |
| `SearchContextOptions` / `SearchContextResult` | options：`search_type`、`image`、`node_limit`、`score_threshold`、`filter`、`context_type`、`include_provenance`、`tags`、`since`、`until`、`time_field`、`events_time_decay_protection`、`query_expansion`（`off` / `auto`）、`max_tokens`、`quotas`、`purpose`（`chat` / `coding`）、`detail`、`dedup_turns`、`exclude_uris`、`peer_scope`（`actor` / `all`）、`other_peer_penalty`、`rewrite`、`rewrite_max_bullets`、`telemetry`、`extra`。`SearchContextResult` 为 `total=False`：已建模的可选字段是 `entries`、`rendered`、`digest`、`stats`，它们均可缺失；每个 `SearchContextEntry` 也为 `total=False`，可有 `uri`、`category`、`score`、`detail`、`text`、`origin`。 |
| 导入/写入 options | `AddResourceOptions`：`acl`、`reason`、`instruction`、`create_parent`、`strict`、`ignore_dirs`、`include`、`exclude`、`directly_upload_media`、`preserve_structure`、`watch_interval`、`args`、`telemetry`、`processing_mode`（`semantic_and_vectors` / `vectors_only`）、`add_type`、`tags`、`tag_mode`（`replace` / `append` / `clear`）、`extra`。`AddSkillOptions`：`telemetry`、`target_uri`、`extra`；`UpdateSkillOptions` 另有 `source_metadata`。`WriteOptions`：`acl`、`telemetry`、`processing_mode`、`tags`、`tag_mode`、`extra`；`BatchWriteOptions`：`telemetry`、`skip_conflicts`、`extra`。 |
| 其他内容 options | `CompileOptions`：`instruction`、`args`、`extra`；`SetTagsOptions`：`telemetry`、`extra`；`ReindexOptions`：`force`、`tags`、`tag_mode`、`extra`。 |
| 分页形状 | `ListPage` 的稳定键为 `result: List[Any]`、`has_more: bool`；`TreePage` 为 `result: List[Dict[str, Any]]`、`has_more: bool`。SDK 会在缺失时分别归一为 `[]`、`False`；节点字典内部不承诺字段。 |
| Session 配置 | `CreateSessionOptions`：`memory_policy`、`auto_commit_policy`、`memory_extraction_config`、`telemetry`、`extra`；`UpdateSessionConfigOptions`：`auto_commit_policy`、`memory_extraction_config`、`telemetry`、`extra`。只有 `auto_commit_policy` 在**键存在**时保留 `None` 为显式 `null`；其它普通 `None` 省略。 |
| 消息形状 | `Message` 仅 `role` 必填；可选 `content`、`parts`、`created_at`、`peer_id`、`turn_id`、`message_kind`（`user_query` / `assistant_step` / `tool_transport` / `checkpoint`）、`source_message_ids`、`telemetry`。`AddMessageOptions` 是除 `content`/`parts` 外的相同元数据加 `extra`；`BatchAddMessagesOptions` 为 `telemetry`、`extra`。 |
| commit 配置 | `CommitSessionOptions`：`enable_working_memory`、`retention_mode`（`turn_budget`）、`keep_recent_turn_count`、`retained_message_token_budget`、`min_raw_tail_steps`、`event_tags`、`telemetry`、`extra`。只要任一 turn 保留字段存在，必须同时设 `retention_mode="turn_budget"`；`event_tags=[]` 会显式写入空 tags。 |
| Experience / 资产 | `ExperienceTrajectoryOptions`：`limit`、`offset`、`start_date`、`end_date`；`ExperienceOutcomeOptions`：`start_date`、`end_date`；`ResolveAssetsOptions`：`catalog_yaml`、`manifest_label`、`catalog_label`、`extra`；`PreflightAssetOptions`：`branch`、`commit`、`auth_config`、`extra`，其中 `AssetGitAuth` 可有 `username`、`token`。 |
| 任务 | SDK 未定义 Task TypedDict。`get_task` 的 404 为 `None`，否则任务字典；`list_tasks` 为任务字典列表。`status` 等服务端字段均是原样映射，不应把未在 SDK 类型中定义的键作为稳定契约。 |
| 错误 | 所有 SDK 映射错误继承 `OpenVikingError`，稳定实例属性为 `message`、`code`、`details`。具体类型为 `InvalidArgumentError`、`InvalidURIError`、`NotFoundError`、`AlreadyExistsError`、`ConflictError`、`FailedPreconditionError`、`AbortedError`、`UnauthenticatedError`、`PermissionDeniedError`、`UnavailableError`、`ResourceExhaustedError`、`InternalError`、`DeadlineExceededError`、`UnimplementedError`、`ProcessingError`、`EmbeddingFailedError`、`VLMFailedError`、`SessionExpiredError`、`NotInitializedError`。 |

## 兼容性、升级与开发

SDK 支持 Python 3.8+，其发布版本独立于 OpenViking Server 主包。升级时请阅读 release notes，重点复核后台任务默认值、`None`/省略字段语义和目标服务端版本是否支持所用能力。旧 `user_id`、`agent_id` 参数当前仍为兼容别名，新代码使用 `user`、`actor_peer_id`。

从源码开发和验证：

```bash
cd sdk/python
pip install -e .
uv run --active pytest
```

SDK 版本 tag 使用 `python-sdk@<version>`，例如 `python-sdk@0.1.3`；它与主包的 `v<version>` tag 命名空间独立。
