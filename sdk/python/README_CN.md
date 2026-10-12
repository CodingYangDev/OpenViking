# OpenViking Python SDK

[English](README.md) | 中文

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

本表列的是 `SyncHTTPClient` 的公开面；`AsyncHTTPClient` 同名网络方法应 `await`，而 `session(...)` 仍是同步地创建 wrapper。除下面注明的例外，`None` 参数和未放入 `options` 的键都会省略；`options.extra` 是受控扩展字段，不能覆盖固定参数。普通默认值会传给服务端。`auto_commit_policy` 是特例：在 `CreateSessionOptions` / `UpdateSessionConfigOptions` 中**键存在**且值为 `None` 时发送 JSON `null`（禁用策略），键缺失才是省略。

| 参数 | 含义 |
|---|---|
| `name=value` | 下表中 `` `name` `` 是实际位置或关键字参数，`value` 是默认值；没有默认值即必填。 |
| `None` | 除表中明确的 `auto_commit_policy` 和 `wait_processed.timeout` 外，表示不发该请求字段。 |

| 方法 | 参数含义（每项均按签名顺序） | 返回 |
|---|---|---|
| `initialize()` / `close()` | 无参数；初始化 / 关闭底层 HTTP client。 | `None` |
| `session(session_id=None, must_exist=False)`；`session_exists(session_id)` | `session_id`：绑定的 Session ID，`None` 创建空 ID wrapper；`must_exist`：仅同步 client 为 `True` 时预读 Session 并在不存在时失败；`session_exists` 的 `session_id` 是待检查 ID。异步 `session` 接受 `must_exist` 但当前不检查。 | `SyncSession` / `bool` |
| `add_resource(path, to=None, parent=None, wait=False, timeout=None, options=None)` | `path`：本地文件/目录或服务端可识别来源；`to`：精确目标 URI；`parent`：导入父目录（不能与 `to` 同用）；`wait`：是否等待处理；`timeout`：等待秒数，`None` 不传；`options`：`AddResourceOptions`。 | 原样 `Dict` |
| `add_skill(data, wait=False, timeout=None, options=None)`；`update_skill(skill_name, data, wait=False, timeout=None, options=None)` | `skill_name`：更新目标；`data`：技能数据、文件或目录；`wait` / `timeout`：是否等待及其秒数；`options`：新增用 `AddSkillOptions`，更新用 `UpdateSkillOptions`。 | 原样 `Dict` |
| `list_skills(node_limit=1000, target_uri=None)`；`find_skills(query, limit=10, score_threshold=None, level=None, telemetry=False, target_uri=None)` | `node_limit`：遍历节点上限；`target_uri`：技能根；`query`：检索词；`limit`：结果上限；`score_threshold`：最低分；`level`：层级筛选；`telemetry`：遥测上下文。 | 原样 `Dict` |
| `validate_skill(data, strict=False, source_path=None, skill_dir_name=None, target_uri=None)`；`get_skill(skill_name, include_content=None, include_files=True, include_source=False, level=None, target_uri=None, include_integrity=False)`；`delete_skill(skill_name, target_uri=None)` | `data`：待校验技能；`strict`：严格校验；`source_path` / `skill_dir_name`：来源路径/目录名；`skill_name`：目标技能；`include_content`：`None` 不传，其他值请求内容；`include_files` / `include_source` / `include_integrity`：是否返回对应材料；`level`：返回层级；`target_uri`：技能根。 | 原样 `Dict` |
| `list_watches(active_only=False, to_uri=None)`；`get_watch(task_id, to_uri=None)`；`update_watch(task_id=None, *, to_uri=None, watch_interval=None, is_active=None, reason=None, instruction=None)`；`delete_watch(task_id=None, *, to_uri=None)`；`trigger_watch(task_id=None, *, to_uri=None)` | `active_only`：仅活跃 Watch；`task_id`：Watch 任务；`to_uri`：目标 URI，也可作为任务替代定位；后三者至少给其一；`watch_interval`：轮询间隔；`is_active`：启停；`reason` / `instruction`：变更原因/处理指令。更新的 `None` 均省略。 | 原样 `Dict` |
| `wait_processed(timeout=None)` | `timeout`：服务端等待秒数；请求总含该键，`None` 发 `null`、`0` 发 `0`；HTTP 读超时用 `timeout if timeout else 600.0`，故两者均为 600 秒。它只等系统处理完成，**不是**指定任务的完成证明。 | 原样 `Dict` |
| `ls(uri, simple=False, recursive=False, output="original", abs_limit=256, show_all_hidden=False, node_limit=1000, sort_by=None, sort_order="asc", extra_fields=None, tags=None, include_tags=False, offset=0, limit=None, include_abstract=None, include_overview=None, overview_limit=4000)`；`ls_page(...)` | `uri`：目录；`simple`：简化节点；`recursive`：递归；`output`：输出格式；`abs_limit` / `node_limit`：路径解析/节点上限；`show_all_hidden`：含隐藏项；`sort_by` / `sort_order`：排序键/方向；`extra_fields`：额外节点字段；`tags` / `include_tags`：按 tags 筛选/返回 tags；`offset` / `limit`：分页；`include_abstract` / `include_overview`：附带摘要/概述；`overview_limit`：概述长度上限。`ls` 只返回 `result`；`ls_page` 返回整页，所有参数是直接命名参数而非 options。 | `List[Any]` / `ListPage` |
| `tree(uri, output="original", abs_limit=128, show_all_hidden=False, node_limit=1000, level_limit=3, extra_fields=None, tags=None, include_tags=False, offset=0, limit=None, include_abstract=None, include_overview=None, overview_limit=4000, directories_only=False)`；`tree_page(...)` | 参数与 `ls` 同义；`level_limit`：树深度；`directories_only`：仅目录。`tree` 返回结果列表，`tree_page` 返回整页。 | `List[Dict]` / `TreePage` |
| `stat(uri)`；`attrs(uri)`；`mkdir(uri, description=None, *, acl=None)`；`rm(uri, recursive=False, wait=False, timeout=None)`；`mv(from_uri, to_uri)` | `uri`：节点/目录；`description`：新目录说明；`acl`：新目录 ACL；`recursive`：递归删除；`wait` / `timeout`：删除是否等待/秒数；`from_uri` / `to_uri`：移动源/目标。 | 原样 `Dict` / `None` |
| `read(uri, offset=0, limit=-1)`；`read_raw(uri, offset=0, limit=-1)`；`download_bytes(uri)`；`abstract(uri)`；`overview(uri)` | `uri`：内容 URI；`offset`：读取起点；`limit`：读取长度，`-1` 为 SDK 默认；`read` 为处理后文本，`read_raw` 为原始文本，`download_bytes` 为字节，后两者为语义摘要/概述。 | `str` / `bytes` |
| `write(uri, content, mode="replace", wait=False, timeout=None, options=None)`；`batch_write(root_uri, operations, wait=True, timeout=None, options=None)` | `uri` / `root_uri`：单写 URI / 批操作根；`content`：文本；`mode`：写入模式；`operations`：逐项写操作；`wait`：单写默认不等、批写默认等；`timeout`：等待秒数；`options`：`WriteOptions` / `BatchWriteOptions`。 | 原样 `Dict` |
| `set_tags(uri, tags=None, mode="replace", recursive=False, options=None)`；ACL：`acl_get(uri)`、`acl_set(uri, entries=None, *, acl_mode=None)`、`acl_grant(uri, principal, level)`、`acl_revoke(uri, principal)`、`acl_delete(uri)` | `tags`：tag 列表，`None` 不写入；`mode`：`replace` / `append` / `clear`；`recursive`：递归；`entries`：ACL 条目；`acl_mode`：`inherit` / `restricted`，和 `entries` 至少一个；`principal`：主体；`level`：授予等级。 | 原样 `Dict` |
| `find(query="", target_uri="", limit=10, image=None, options=None)`；`search(query="", session_id=None, target_uri="", limit=10, image=None, options=None)`；`search_context(...)` | `query`：检索词；`session_id`：搜索关联 Session；`target_uri`：一个或多个目标 URI；`limit`：结果数；`image`：图像输入，非空才写入；`options`：`FindOptions` / `SearchOptions` / `SearchContextOptions`。`search_context` 参数同 `search`，但固定 context 模式。 | 原样 `Dict` / `SearchContextResult` |
| `grep(uri, pattern, case_insensitive=False, node_limit=256, exclude_uri=None, tags=None, include_tags=False, level_limit=None)`；`glob(pattern, uri="viking://", node_limit=256, extra_fields=None, tags=None, include_tags=False)` | `pattern`：文本正则 / 路径 glob；`uri`：搜索根；`case_insensitive`：忽略大小写；`node_limit`：扫描上限；`exclude_uri`：排除根；`tags` / `include_tags`：筛选/返回 tags；`level_limit`：grep 层级上限；`extra_fields`：glob 返回字段。 | 原样 `Dict` |
| `create_session(session_id=None, options=None)`；`list_sessions()`；`get_session(session_id, *, auto_create=False)`；`update_session_config(session_id, options=None)`；`delete_session(session_id)` | `session_id`：指定 ID，创建时 `None` 交服务端生成；`auto_create`：读取缺失时创建；`options`：创建/更新配置。`auto_commit_policy=None` 的显式 null 规则见表首。 | 原样 `Dict` / `List` / `None` |
| `get_session_context(session_id, token_budget=128_000)`；`get_session_archive(session_id, archive_id)`；`add_message(session_id, role, content=None, parts=None, options=None, peer_id=None)`；`batch_add_messages(session_id, messages, options=None)`；`commit_session(session_id, keep_recent_count=0, options=None)` | `token_budget`：上下文 token 预算；`archive_id`：归档；`role`：消息角色；`content` / `parts`：文本或结构化分段，至少一项有效；`peer_id`：消息主体，不能与 options 内同名键并用；`messages`：消息列表；`keep_recent_count`：commit 保留最近消息数；`options`：对应消息/commit options。 | 原样 `Dict` |
| `get_task(task_id)`；`list_tasks(task_type=None, status=None, resource_id=None, limit=50)`；`cancel_task(task_id)`；`compile(from_uris, to, skill, options=None)` | `task_id`：任务；`task_type` / `status` / `resource_id`：任务筛选；`limit`：列表上限；`from_uris`：编译输入；`to`：输出 URI；`skill`：使用技能；`options`：`CompileOptions`。`get_task` 的 404 返回 `None`。 | `Optional[Dict]` / `List[Dict]` / 原样 `Dict` |
| `export_ovpack(uri, to, include_vectors=False)`；`backup_ovpack(to, include_vectors=False)`；`import_ovpack(file_path, parent, on_conflict=None, vector_mode=None)`；`restore_ovpack(file_path, on_conflict=None, vector_mode=None)`；`check_consistency(uri)` | `to`：本地输出路径；`include_vectors`：打包向量；`file_path`：本地包文件；`parent`：导入父 URI；`on_conflict`：冲突策略；`vector_mode`：向量处理模式；`uri`：一致性检查根。 | 本地路径 `str` / 服务端 URI `str` / 原样 `Dict` |
| `reindex(uri, mode="vectors_only", wait=True, recursive=True, force=False, options=None)`；`health()`；`is_healthy()` | `uri`：重建根；`mode`：默认仅向量；`wait`：默认等待；`recursive`：默认递归；`force`：强制重建，`False` 时不写入；`options`：`ReindexOptions`。健康方法无参数。 | 原样 `Dict` / `bool` |
| `queue_status(format=None)`、`vikingdb_status(format=None)`、`models_status(format=None)`、`get_status(format=None)`；`observer` | `format`：`None`、`"table"` 或 `"json"`；`observer.queue` / `.vikingdb` / `.models` / `.system` 为无参同类状态，`.is_healthy()` 返回 bool。 | 原样 `Dict` / `bool` |
| 管理：`admin_create_account(account_id, admin_user_id, user_config=None, seed=None)`；`admin_list_accounts(name=None, limit=None, page=1, query=None)`；`admin_delete_account(account_id)` | `account_id`：账户；`admin_user_id`：初始管理员；`user_config`：用户配置；`seed`：密钥种子；`name` / `query`：筛选；`limit` / `page`：分页（仅指定 limit 才发送 page）。这些接口需要 root key。 | 原样 `Dict` / `List` |
| 管理：`admin_register_user(account_id, user_id, role="user", user_config=None, seed=None)`；`admin_list_users(account_id, limit=None, name=None, role=None, page=1, query=None)`；`admin_remove_user(account_id, user_id)`；`admin_set_role(account_id, user_id, role)`；`admin_regenerate_key(account_id, user_id, seed=None)` | `account_id` / `user_id`：账户和用户；`role`：注册默认 `user` 或筛选/设置角色；`user_config` / `seed`：配置/密钥种子；`limit` / `page` / `name` / `query`：列表分页和筛选。 | 原样 `Dict` / `List` |
| 管理：`admin_create_group(account_id, group_id)`、`admin_list_groups(account_id)`、`admin_delete_group(account_id, group_id)`、`admin_list_group_members(account_id, group_id)`、`admin_add_group_member(account_id, group_id, user_id)`、`admin_remove_group_member(account_id, group_id, user_id)`、`admin_migrate(cleanup=False)` | `account_id` / `group_id` / `user_id`：账户、组、成员；`cleanup`：`False` 执行迁移，`True` 执行清理。 | 原样 `Dict` / `List` |
| Settings / 资产：`admin_get_agent_evolution()`、`admin_set_agent_evolution(enabled)`、`admin_get_account_settings(account_id)`、`admin_set_account_agent_evolution(account_id, enabled)`；`list_experience_trajectories(experience_uri, options=None)`、`get_experience_outcomes(experience_uri, options=None)`、`resolve_openviking_assets(manifest_yaml, options=None)`、`preflight_openviking_asset(name, repo_url, options=None)` | `enabled`：演进开关；`account_id`：账户；`experience_uri`：experience 根；`manifest_yaml`：资产清单 YAML；`name` / `repo_url`：资产名/Git 地址；`options`：各自 Experience / Assets Options。 | 原样 `Dict` |
| `snapshot.commit(*, message, paths=None, branch="main", author_name=None, author_email=None)`；`snapshot.restore(*, project_dir=None, source_commit, branch="main", dry_run=False, message=None, author_name=None, author_email=None)` | `message`：提交/恢复说明；`paths`：仅提交路径；`branch`：分支；`author_name` / `author_email`：提交作者；`project_dir`：恢复目录；`source_commit`：恢复源提交；`dry_run`：仅预演。`*` 后参数必须用关键字。 | 原样 `Dict` |
| `snapshot.show(target_ref, *, path=None)`；`snapshot.log(*, branch="main", limit=20, paths=None)`；`snapshot.diff(path, *, to_ref, from_ref=None)`；`snapshot.get_gitignore()`、`set_gitignore(*, content)`、`delete_gitignore()` | `target_ref`：提交/引用；`path`：展示或比较文件；`branch` / `limit` / `paths`：日志范围；`to_ref` / `from_ref`：diff 两端；`content`：gitignore 文本。`show` 有 `path` 时二进制分支稳定含 `oid`、`size`、`bytes`。同步 client 还透传同签名 `git_*` 别名。 | `Any` / `List[Dict]` / `Dict` / `str` / `None` |
| `SyncSession.add_message(role, content=None, parts=None, options=None, peer_id=None)`；`batch_add_messages(messages)`；`commit(keep_recent_count=0, options=None)`；`commit_async(keep_recent_count=0, options=None)`；`delete()`；`load()`；`get_session_context(token_budget=128_000)`；`get_archive(archive_id)` | wrapper 固定使用其 `session_id`，其他参数与同名 client 方法同义。`commit_async` **仅 SyncSession 有**，仍同步执行且是 `commit` 别名；异步 `Session` 只有 `await commit(...)`。 | 原样 `Dict` / `None` |

### 字段契约

以下每行是一个 TypedDict 字段；`total=False` 的字段默认都是“省略”，除另注外传入 `None` 会被去除。`Dict[str, Any]`、`Any` 和 `extra` 的内部结构不由 SDK 固定。

| TypedDict | 字段 | 类型 | 默认或省略 | 含义 |
|---|---|---|---|---|
| `FindOptions` | `image` | `Any` | 省略 | 图像检索输入，序列化为 `image_url`。 |
| `FindOptions` | `node_limit` | `int` | 省略 | 检索遍历节点上限。 |
| `FindOptions` | `score_threshold` | `float` | 省略 | 最低匹配分数。 |
| `FindOptions` | `filter` | `Dict[str, Any]` | 省略 | 服务端结构化过滤条件。 |
| `FindOptions` | `context_type` | `Any` | 省略 | 检索上下文类型（枚举会转值）。 |
| `FindOptions` | `include_provenance` | `bool` | 省略 | 是否包含来源信息。 |
| `FindOptions` | `tags` | `List[str]` | 省略 | tags 筛选。 |
| `FindOptions` | `since` | `str` | 省略 | 时间范围起点。 |
| `FindOptions` | `until` | `str` | 省略 | 时间范围终点。 |
| `FindOptions` | `time_field` | `"updated_at" \| "created_at"` | 省略 | 应用时间范围的字段。 |
| `FindOptions` | `level` | `int \| str \| List[int]` | 省略 | 节点层级筛选。 |
| `FindOptions` | `read_content` | `bool` | 省略 | 是否读取命中内容。 |
| `FindOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `FindOptions` | `events_time_decay_protection` | `Optional[str]` | `None` 省略 | 事件时间衰减保护配置。 |
| `FindOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `SearchOptions` | `search_type` | `"semantic" \| "keywords"` | 省略 | 语义或关键词搜索。 |
| `SearchOptions` | `extra` | `Mapping[str, Any]` | 省略 | 从 `FindOptions` 继承的受控透传字段。 |
| `SearchContextOptions` | `search_type` | `"semantic" \| "keywords"` | 省略 | 搜索方式。 |
| `SearchContextOptions` | `image` | `Any` | 省略 | 图像检索输入。 |
| `SearchContextOptions` | `node_limit` | `int` | 省略 | 检索遍历节点上限。 |
| `SearchContextOptions` | `score_threshold` | `float` | 省略 | 最低匹配分数。 |
| `SearchContextOptions` | `filter` | `Dict[str, Any]` | 省略 | 结构化过滤条件。 |
| `SearchContextOptions` | `context_type` | `Any` | 省略 | 检索上下文类型。 |
| `SearchContextOptions` | `include_provenance` | `bool` | 省略 | 是否包含来源。 |
| `SearchContextOptions` | `tags` | `List[str]` | 省略 | tags 筛选。 |
| `SearchContextOptions` | `since` | `str` | 省略 | 时间范围起点。 |
| `SearchContextOptions` | `until` | `str` | 省略 | 时间范围终点。 |
| `SearchContextOptions` | `time_field` | `"updated_at" \| "created_at"` | 省略 | 应用时间范围的字段。 |
| `SearchContextOptions` | `events_time_decay_protection` | `Optional[str]` | `None` 省略 | 事件时间衰减保护。 |
| `SearchContextOptions` | `query_expansion` | `"off" \| "auto"` | 省略 | 查询扩展策略。 |
| `SearchContextOptions` | `max_tokens` | `int` | 省略 | 组装上下文 token 上限。 |
| `SearchContextOptions` | `quotas` | `Dict[str, int]` | 省略 | 分类配额。 |
| `SearchContextOptions` | `purpose` | `"chat" \| "coding"` | 省略 | 上下文用途。 |
| `SearchContextOptions` | `detail` | `str \| Dict[str, str]` | 省略 | 输出细节等级或分类细节。 |
| `SearchContextOptions` | `dedup_turns` | `int` | 省略 | 去重的最近轮数。 |
| `SearchContextOptions` | `exclude_uris` | `List[str]` | 省略 | 排除 URI。 |
| `SearchContextOptions` | `peer_scope` | `"actor" \| "all"` | 省略 | 检索主体范围。 |
| `SearchContextOptions` | `other_peer_penalty` | `float \| Dict[str, float]` | 省略 | 其他主体结果惩罚。 |
| `SearchContextOptions` | `rewrite` | `bool \| "auto"` | 省略 | 是否改写结果。 |
| `SearchContextOptions` | `rewrite_max_bullets` | `int` | 省略 | 改写要点上限。 |
| `SearchContextOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `SearchContextOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `AddResourceOptions` | `acl` | `Dict[str, Any]` | 省略 | 导入资源 ACL。 |
| `AddResourceOptions` | `reason` | `str` | 省略 | 导入原因。 |
| `AddResourceOptions` | `instruction` | `str` | 省略 | 处理指令。 |
| `AddResourceOptions` | `create_parent` | `bool` | 省略 | 是否创建父目录。 |
| `AddResourceOptions` | `strict` | `bool` | 省略 | 是否严格处理失败。 |
| `AddResourceOptions` | `ignore_dirs` | `str` | 省略 | 忽略目录模式。 |
| `AddResourceOptions` | `include` | `str` | 省略 | 包含模式。 |
| `AddResourceOptions` | `exclude` | `str` | 省略 | 排除模式。 |
| `AddResourceOptions` | `directly_upload_media` | `bool` | 省略 | 是否直接上传媒体。 |
| `AddResourceOptions` | `preserve_structure` | `bool` | 省略 | 是否保留来源目录结构。 |
| `AddResourceOptions` | `watch_interval` | `float` | 省略 | Watch 轮询秒数。 |
| `AddResourceOptions` | `args` | `Dict[str, Any]` | 空字典也省略 | connector/处理器参数。 |
| `AddResourceOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `AddResourceOptions` | `processing_mode` | `"semantic_and_vectors" \| "vectors_only"` | 省略 | 语义+向量或仅向量处理。 |
| `AddResourceOptions` | `add_type` | `str` | 省略 | 特殊导入类型；与 `parent` 互斥且要求精确 `to`。 |
| `AddResourceOptions` | `tags` | `List[str]` | 省略 | 写入的 tags。 |
| `AddResourceOptions` | `tag_mode` | `"replace" \| "append" \| "clear"` | 省略 | tags 合并方式。 |
| `AddResourceOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `AddSkillOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `AddSkillOptions` | `target_uri` | `str` | 省略 | 技能目标根 URI。 |
| `AddSkillOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `UpdateSkillOptions` | `source_metadata` | `Dict[str, Any]` | 省略 | 更新的来源元数据；继承 `AddSkillOptions`。 |
| `UpdateSkillOptions` | `extra` | `Mapping[str, Any]` | 省略 | 从 `AddSkillOptions` 继承的受控透传字段。 |
| `WriteOptions` | `acl` | `Dict[str, Any]` | 省略 | 写入 ACL。 |
| `WriteOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `WriteOptions` | `processing_mode` | `"semantic_and_vectors" \| "vectors_only"` | 省略 | 写入处理模式。 |
| `WriteOptions` | `tags` | `List[str]` | 省略 | 写入 tags。 |
| `WriteOptions` | `tag_mode` | `"replace" \| "append" \| "clear"` | 省略 | tags 合并方式。 |
| `WriteOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `BatchWriteOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `BatchWriteOptions` | `skip_conflicts` | `bool` | 省略 | 返回冲突而不覆盖或阻塞兼容写入。 |
| `BatchWriteOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `CompileOptions` | `instruction` | `str` | 省略 | 编译指令。 |
| `CompileOptions` | `args` | `Dict[str, Any]` | 空字典也省略 | 编译处理参数。 |
| `CompileOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `SetTagsOptions` | `telemetry` | `Any` | 省略 | tags 操作遥测。 |
| `SetTagsOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `ReindexOptions` | `force` | `bool` | 省略 | 强制重建。 |
| `ReindexOptions` | `tags` | `List[str]` | 省略 | 重建同步的 tags。 |
| `ReindexOptions` | `tag_mode` | `"replace" \| "append" \| "clear"` | 省略 | tags 合并方式。 |
| `ReindexOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段；不可覆盖 SDK 字段。 |
| `ListPage` | `result` | `List[Any]` | 必有；缺失归一为 `[]` | 当前页节点。 |
| `ListPage` | `has_more` | `bool` | 必有；缺失归一为 `False` | 是否还有下一页。 |
| `TreePage` | `result` | `List[Dict[str, Any]]` | 必有；缺失归一为 `[]` | 当前页树节点。 |
| `TreePage` | `has_more` | `bool` | 必有；缺失归一为 `False` | 是否还有下一页。 |
| `CreateSessionOptions` | `memory_policy` | `Dict[str, Any]` | 省略 | 初始记忆策略。 |
| `CreateSessionOptions` | `auto_commit_policy` | `Optional[Dict[str, Any]]` | 缺键省略；存在且 `None` 发 `null` | 自动 commit 策略；`null` 明确禁用。 |
| `CreateSessionOptions` | `memory_extraction_config` | `Dict[str, Any]` | 省略 | 初始抽取配置。 |
| `CreateSessionOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `CreateSessionOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段。 |
| `UpdateSessionConfigOptions` | `auto_commit_policy` | `Optional[Dict[str, Any]]` | 缺键省略；存在且 `None` 发 `null` | 自动 commit 策略；`null` 明确禁用。 |
| `UpdateSessionConfigOptions` | `memory_extraction_config` | `Dict[str, Any]` | 省略 | 更新抽取配置。 |
| `UpdateSessionConfigOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `UpdateSessionConfigOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段。 |
| `Message` | `role` | `str` | **必填** | 消息角色。 |
| `Message` | `content` | `str` | 省略；实际消息至少一项有效 | 文本内容。 |
| `Message` | `parts` | `List[Any]` | 省略；实际消息至少一项有效 | 结构化内容分段。 |
| `Message` | `created_at` | `str` | 省略 | 创建时间。 |
| `Message` | `peer_id` | `str` | 省略 | 消息主体。 |
| `Message` | `turn_id` | `str` | 省略 | 会话轮次。 |
| `Message` | `message_kind` | `"user_query" \| "assistant_step" \| "tool_transport" \| "checkpoint"` | 省略 | 消息类别。 |
| `Message` | `source_message_ids` | `List[str]` | 省略 | 来源消息 ID。 |
| `Message` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `AddMessageOptions` | `created_at` | `str` | 省略 | 消息创建时间。 |
| `AddMessageOptions` | `peer_id` | `str` | 省略 | 消息主体。 |
| `AddMessageOptions` | `turn_id` | `str` | 省略 | 会话轮次。 |
| `AddMessageOptions` | `message_kind` | `"user_query" \| "assistant_step" \| "tool_transport" \| "checkpoint"` | 省略 | 消息类别。 |
| `AddMessageOptions` | `source_message_ids` | `List[str]` | 省略 | 来源消息 ID。 |
| `AddMessageOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `AddMessageOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传字段。 |
| `BatchAddMessagesOptions` | `telemetry` | `Any` | 省略 | 批写遥测。 |
| `BatchAddMessagesOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段。 |
| `CommitSessionOptions` | `enable_working_memory` | `Optional[bool]` | `None` 省略 | 是否启用工作记忆。 |
| `CommitSessionOptions` | `retention_mode` | `"turn_budget"` | 省略 | 启用 turn 保留字段时必填的模式。 |
| `CommitSessionOptions` | `keep_recent_turn_count` | `int` | 省略 | 保留的最近轮数。 |
| `CommitSessionOptions` | `retained_message_token_budget` | `int` | 省略 | 保留消息的 token 预算。 |
| `CommitSessionOptions` | `min_raw_tail_steps` | `int` | 省略 | 原始尾部最少步骤数。 |
| `CommitSessionOptions` | `event_tags` | `List[str]` | 省略；`[]` 显式发送 | 抽取事件 tags。 |
| `CommitSessionOptions` | `telemetry` | `Any` | 省略 | 遥测上下文。 |
| `CommitSessionOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段。 |
| `ExperienceTrajectoryOptions` | `limit` | `int` | 省略 | 轨迹页大小。 |
| `ExperienceTrajectoryOptions` | `offset` | `int` | 省略 | 轨迹分页偏移。 |
| `ExperienceTrajectoryOptions` | `start_date` | `str` | 省略 | 轨迹日期范围起点。 |
| `ExperienceTrajectoryOptions` | `end_date` | `str` | 省略 | 轨迹日期范围终点。 |
| `ExperienceOutcomeOptions` | `start_date` | `str` | 省略 | outcome 日期范围起点。 |
| `ExperienceOutcomeOptions` | `end_date` | `str` | 省略 | outcome 日期范围终点。 |
| `ResolveAssetsOptions` | `catalog_yaml` | `str` | 省略 | 资产目录 YAML。 |
| `ResolveAssetsOptions` | `manifest_label` | `str` | 省略 | manifest 标签。 |
| `ResolveAssetsOptions` | `catalog_label` | `str` | 省略 | 目录标签。 |
| `ResolveAssetsOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段。 |
| `AssetGitAuth` | `username` | `str` | 省略 | Git 仓库认证用户名。 |
| `AssetGitAuth` | `token` | `str` | 省略 | Git 仓库认证凭据。 |
| `PreflightAssetOptions` | `branch` | `str` | 省略 | 预检 Git 分支。 |
| `PreflightAssetOptions` | `commit` | `str` | 省略 | 预检 Git 提交。 |
| `PreflightAssetOptions` | `auth_config` | `AssetGitAuth` | 省略 | Git 仓库认证配置。 |
| `PreflightAssetOptions` | `extra` | `Mapping[str, Any]` | 省略 | 受控透传的新服务端字段。 |
| `SearchContextEntry` | `uri` | `str` | 省略 | 上下文条目的 URI。 |
| `SearchContextEntry` | `category` | `str` | 省略 | 上下文条目分类。 |
| `SearchContextEntry` | `score` | `float` | 省略 | 命中得分。 |
| `SearchContextEntry` | `detail` | `str` | 省略 | 条目细节。 |
| `SearchContextEntry` | `text` | `str` | 省略 | 条目文本。 |
| `SearchContextEntry` | `origin` | `str` | 省略 | 条目来源。 |
| `SearchContextResult` | `entries` | `List[SearchContextEntry]` | 省略 | 上下文条目。 |
| `SearchContextResult` | `rendered` | `str` | 省略 | 渲染后的上下文文本。 |
| `SearchContextResult` | `digest` | `str` | 省略 | 上下文摘要。 |
| `SearchContextResult` | `stats` | `Dict[str, Any]` | 省略 | 上下文统计。 |

`Task` 没有 SDK TypedDict：`get_task` 的 404 是 `None`，其余任务字典是服务端原样映射。所有映射错误继承 `OpenVikingError`，稳定实例属性为 `message`、`code`、`details`；具体类型见 `openviking_sdk.errors`。

## 兼容性、升级与开发

SDK 支持 Python 3.8+，其发布版本独立于 OpenViking Server 主包。升级时请阅读 release notes，重点复核后台任务默认值、`None`/省略字段语义和目标服务端版本是否支持所用能力。旧 `user_id`、`agent_id` 参数当前仍为兼容别名，新代码使用 `user`、`actor_peer_id`。

从源码开发和验证：

```bash
cd sdk/python
pip install -e .
uv run --active pytest
```

SDK 版本 tag 使用 `python-sdk@<version>`，例如 `python-sdk@0.1.3`；它与主包的 `v<version>` tag 命名空间独立。
