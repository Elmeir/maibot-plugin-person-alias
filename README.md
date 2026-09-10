# 人物称呼与别名插件

给 MaiBot 加两件事，全部通过插件 SDK 实现，**不改宿主一行代码**：

1. **注入**：把「主称呼 + 别名」（含 WebUI 里维护的人工别名）注入 Maisaka planner 上下文，让麦麦知道当前在说谁。
2. **换名**：把运行时显示的 **QQ 昵称 / 群名片**替换成你自己维护的称呼，planner、回复器、日志、WebUI 监控面板看到的名字就都统一了。

## 安装

```bash
cd /你的部署目录/plugins    # 例如 /opt/MaiBot/plugins
git clone https://github.com/Elmeir/maibot-plugin-person-alias.git person_profile_name_alias
# 重启麦麦（systemctl restart xxx / docker compose restart / 重启 bot.py）
```

> 目录名必须保持 `person_profile_name_alias`：插件配置存在插件目录内的 `config.toml`，
> 换目录名会丢配置。

更新：`cd plugins/person_profile_name_alias && git pull` 后重启。

### 自检（可选）

```bash
python verify_plugin.py /你的MaiBot仓库根     # 例如 python verify_plugin.py /opt/MaiBot
```

不联网也能跑：它用假宿主把插件加载一遍，覆盖注入、换名、配置迁移等全部路径。

重启后到 WebUI 插件页找到「人物称呼与别名注入」并启用，配置项都在插件面板里。配置项都有中文标题与说明，其中「生效范围」「名字来源」是**下拉框**，直接选就行。

## 换名怎么配（「昵称替换」配置段）

| 配置项 | 控件 | 默认 | 说明 |
|---|---|---|---|
| 启用昵称替换 | 开关 | 开 | 是否启用换名 |
| 生效范围 | **下拉** | 入站即改写 | `入站即改写`=改写消息体（planner/回复器/日志/WebUI 全链路生效）；`仅改 planner`=只改写 planner 请求文本，不碰消息体与数据库 |
| 名字来源 | **下拉** | 自动 | `自动`=主称呼优先，未自定义时用第一个人工别名；`仅主称呼`；`仅人工别名` |
| 替换 QQ 昵称 | 开关 | 开 | 替换 QQ 昵称字段（仅「入站即改写」范围有效） |
| 替换群名片 | 开关 | 开 | 替换群名片字段。群聊显示名以群名片优先，**关掉可能导致替换看不到效果** |
| 替换 @ 与引用对象 | 开关 | 开 | 同时替换 @ 与引用里被指向人的昵称 |
| 改写正文里的 @旧名 | 开关 | 开 | 改写正文里的 `@旧名`、`回复了旧名的消息` 两类固定格式 |
| 硬编码覆盖（JSON） | 多行文本 | 空 | 形如 `{"qq:123456789": "老王"}`，优先级最高（没进人物档案也能用） |

「名字从哪来」的优先级：`overrides` → 人物主档案 `person_name` → A_Memorix 人工别名。

> 旧版本配置文件里写的 `message` / `planner` / `auto` / `person_name` / `manual_alias` **不用手动改**，插件读取时会自动迁移成当前的中文取值；无法识别的取值会回退到默认值，不会让配置加载失败。

**只有进了人物档案（`is_known=True`）的人才会被替换**，陌生人保持原样；想给陌生人指定名字就用 `overrides`。

## 用哪种生效范围？

- 想「运行时看到的就是新名字」→ 用 `入站即改写`（默认）。代价：替换后的名字会跟着这条消息写进消息记录；对**新认识**的人，替换后的名字可能被记进人物档案的群名片列表。
- 只想让 planner 看到新名字、数据库一点不碰 → 用 `仅改 planner`。代价：WebUI 监控面板、日志里仍是原昵称。

## 身份怎么认（重要）

**一律用 `platform + user_id`（QQ 号）解析人物身份**，拿不到 QQ 号就保持原样、绝不猜人。

为什么不用名字：`person.get_id_by_name` 底层是按人物主档案 `person_name` 精确匹配，而插件手里的“名字”通常是**群名片**（其次 QQ 昵称）。群名片由本人随意修改，改成别人的名字就会被认成别人，替换名字时会改错对象。

如果你确实遇到拿不到 QQ 号的平台，可以开启配置「允许按名称反查身份（不推荐）」（默认关闭），此时才会在无 QQ 号时按名称兜底。

> 别名列表里仍然包含群名片，但那是「已按 QQ 号确定身份之后」的名称变体，只描述“这个人有哪些叫法”，不参与身份判定。

## 两个核对工具

| 工具 | 参数 | 用途 |
|---|---|---|
| `person_name_replace_lookup` | `user_id`(QQ号) / `platform` / `current_name` | 核对某个 QQ 号运行时会显示成什么称呼 |
| `person_name_alias_lookup` | `user_id`(QQ号 或 person_id) / `platform` | 核对某个人的称呼与别名（含人工别名） |

`person_name_alias_lookup` 传名称会被拒绝并提示改用 QQ 号（除非开启了按名称反查）。例如：对话里问「查一下 123456789 的称呼和别名」。

## 本地自检

```bash
pip install maibot-plugin-sdk
MAIBOT_REPO=/opt/MaiBot python verify_plugin.py
```

会校验 manifest 合法性、组件声明、注入条目能被宿主协议反序列化、**换名后的消息能否被宿主保真反序列化**（含图片二进制），以及**身份解析不会按群名片猜人**。
