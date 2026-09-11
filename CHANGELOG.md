# Changelog

## [1.4.1] - 2026-09-11

### 规范符合性修复

- manifest：`host_application.min_version` 从 1.2.0 抬到 **1.2.4**——插件挂载的
  `chat.receive.after_process` 钩点在 1.2.0~1.2.3 上不存在（钩点缺失 = 注册直接失败）；
  `max_version` 对齐统一模板改为 1.99.99。
- manifest：`license` 改为标准 SPDX 写法 `GPL-3.0-or-later`（原 `GPL-v3.0-or-later`
  非规范标识，README/LICENSE 均为 GPL-3.0）。
- `config_version` 字段补 `hidden/disabled`（用户不可改），默认值改由
  `SUPPORTED_CONFIG_VERSION` 常量提供，与其他插件一致。
- verify_plugin.py 移除硬编码的本机兜底路径，未指定仓库时使用当前目录并提示。

## [1.4.0] - 2026-09-10

### 可观测性：补齐静默失败的诊断盲区

排查「插件已加载、人物也建档，但没有任何效果」时发现：能力调用被宿主**拒绝**（返回
`{"success": false, "error": ...}` 而不是抛异常）时，插件此前完全静默，打开调试日志也看不到
任何线索。本次把这类路径全部补上日志（均受「更多调试日志」开关控制）：

- `person.get_id`：区分「调用异常」「被宿主拒绝」「返回空 person_id」三种情况，并把宿主的
  `error` 原文打进日志；失败时同时打印实际使用的 `platform:user_id`。
- `person.get_value`：被拒绝时打印 `person_id`、字段名与宿主 `error`。
- 身份判定失败时，日志里补上解析出的 `person_id`，便于和数据库里的值直接比对。
- 拆开「没有可用称呼」与「称呼与当前显示名相同」两种情况，不再都表现为静默返回空串。
- 启动时打印一行配置摘要（总开关 / 昵称替换 / 生效范围 / 名字来源 / 调试日志状态），
  确认 WebUI 里的配置真的落到了插件上。

## [1.3.0] - 2026-09-10

### 安全修复：身份解析只认 QQ 号

- 修复：拿不到 QQ 号时，插件曾用「群名片 → QQ 昵称」的顺序去反查人物身份
  （`person.get_id_by_name`，底层是按人物主档案的 `person_name` 精确匹配）。
  群名片是本人可以随意修改的，改成别人的名字就会被认成别人，替换名字时会改错对象。
  现在**身份一律用 `platform + user_id`（QQ 号）解析**，拿不到就保持原样、不猜人。
- 新增配置「允许按名称反查身份（不推荐）」，默认**关闭**；只有显式开启才允许按名称兜底，
  且仅在拿不到 QQ 号时生效。
- 查询工具 `person_name_alias_lookup` 参数由 `keyword`（名称/群名片/person_id）改为
  `user_id`（QQ 号）+ `platform`，同时接受 `person_id`（32 位十六进制）。
  传入名称会被明确拒绝并提示改用 QQ 号（除非开启上述开关）。
- 说明：别名列表里仍然会包含群名片，但那是「已按 QQ 号确定身份之后」的名称变体，
  只影响“这个人有哪些叫法”，不参与身份判定。

## [1.2.0] - 2026-09-10

### 用户感知功能

- 配置页更好用了：
  - 「生效范围」「名字来源」由文本输入框改为**下拉框**，不再需要手打 `message` / `planner` 之类的标识符。
  - 每个配置项都补上了中文标题和说明（宿主的插件配置页只渲染 `label` / `hint`，之前这些字段显示的其实是英文字段名）。
  - 「硬编码覆盖」改为多行文本框并给了示例占位符；人数、时长等数值项带上了允许范围。
- 下拉取值改为中文（`入站即改写` / `仅改 planner`、`自动` / `仅主称呼` / `仅人工别名`），配置文件自解释。

### 开发侧

- `scope` / `name_source` 改用 `Literal` 声明，宿主 SDK 会据此生成 `ui_type=select` 与 `choices`。
- 新增 `_ui_meta()` / `_normalize_choice()`：统一生成字段 UI 元数据；读取配置时把旧版英文取值（`message` / `planner` / `auto` / `person_name` / `manual_alias`）自动迁移为当前中文取值。
- 加入了 `field_validator(mode="before")`：旧配置加载不会因为取值变化而校验失败；无法识别的取值回退默认值而不是抛异常。
- `max_people`(1~8)、`watch_window_seconds`(≥5)、`cache_seconds`(≥0) 补上数值边界。

## [1.1.0] - 2026-09-10

### 用户感知功能

- 新增「昵称替换」：把运行时显示的 QQ 昵称 / 群名片替换成人物档案里的称呼或人工别名，planner、回复器、日志、WebUI 监控面板看到的名字统一。
- 替换范围可切换：`message`（入站即改写，全链路生效）或 `planner`（只改写 planner 请求文本，零副作用）。
- 名字来源可切换：`auto`（主称呼优先，未自定义时用第一个人工别名）/ `person_name` / `manual_alias`。
- 支持 `overrides` 硬编码映射（找不到人物档案也能指定某个 QQ 号显示成什么名字）。
- 新增工具 `person_name_replace_lookup`，可在对话中核对某个 QQ 号最终会显示成什么称呼。
- 修正：未进入人物档案的陌生人不再显示成「未知用户xxxx」占位，改回消息里的原始称呼。

### 开发侧

- `chat.receive.after_process` 钩子现在会按需返回 `modified_kwargs.message`，改写 `user_info.user_nickname` / `user_cardname`、@ 与引用目标昵称，以及正文里的 `@旧名` / `回复了旧名的消息`。
- 仅在确有改写时才回写消息体，避免无谓的序列化往返。
- 仅对人物档案里 `is_known=True` 的人替换；未认识的人保持原样。

## [1.0.0] - 2026-09-10

### 用户感知功能

- 新增「人物称呼与别名注入」：Maisaka planner 在规划前会收到当前会话相关人物的称呼与别名。
- 支持读取 WebUI「别名维护」里人工维护的别名（只读方式访问 A_Memorix 的 metadata.db）。
- 新增工具 `person_name_alias_lookup`，可在对话中直接核对某人的称呼与别名。

### 开发侧

- 订阅 `chat.receive.after_process` 记录最近发言、@、引用涉及的人物。
- 订阅 `maisaka.planner.before_request`，以插入 `UserMessageItem` 的方式注入内部参考消息。
- 全部通过插件 SDK 能力实现，不修改宿主代码。
