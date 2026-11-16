# migrate-agent

完整操作指南见 [USAGE.md](USAGE.md)，包括安装、预览、项目映射、只读验证、冲突处理和回滚。

把 Codex 的会话、上下文、Markdown 规则、记忆和自定义技能迁移到 **DeepSeek Harness**。导入的历史可以继续对话；项目规则和技能也会写入 Harness 原生发现的位置，供新建会话使用。运行中的客户端可能需要重新启动以刷新外部新增历史；已有工作区的会话归属由 Harness 单独管理，导入会话可能先出现在“未分组”，可通过客户端移入对应工作区。

当前实现已对照本机 DeepSeek Harness `0.2.2-rc.3` 随附源码，生成 **Session v4** 原生会话，并使用其原生解析器和 `Session.deriveMessages()` 验证。迁移过程本身不调用模型，也不需要 API Key；真实客户端中的对话验证使用 Harness 已配置的模型。

## 开始使用

需要 Python 3.9 或更新版本。建议先安装完整读取依赖：

```bash
python3 -m pip install -e '.[codex]'
```

然后在本目录运行：

```bash
# 一条命令执行 Codex → DeepSeek Harness
python3 -m migrate_agent migrate

# 先预览，目标目录不会发生写入
python3 -m migrate_agent plan

# 只迁移某个项目的会话，并携带项目规则与 .codex 下的 Markdown
python3 -m migrate_agent migrate --project /absolute/path/to/project
```

macOS 也可以双击 [`migrate.command`](migrate.command) 执行默认迁移，或使用安装后的命令 `migrate-agent migrate`。不安装项目也可以直接运行源码；缺少读取依赖时会报告相应降级。

`[codex]` 为旧版 Python 补充 TOML 解析和 Codex 压缩历史解码。Python 3.14 的标准库或系统 `zstd` 命令也可解码压缩历史；缺少解码器时会尝试 SQLite 历史并明确报告降级。写入 Harness 的 Zstandard 格式不依赖额外库。

默认读取 `CODEX_HOME` 或 `~/.codex`，写入 `DSH_HOME` 或 `~/.dsh`。Codex 数据目录保持只读；项目中会生成或合并 `AGENTS.md`，并将技能放入 `.dsh/skills`，所有修改纳入备份和回滚。操作完成后，可以继续导入的会话，也可以在对应项目根目录新建 Harness 会话；更换电脑时先准备好项目代码，再使用路径映射。

## 常用操作

```bash
# 指定数据目录；适合测试、导出的 Codex home 或另一台机器的数据
python3 -m migrate_agent plan \
  --source-home /data/codex \
  --target-home /data/deepseek \
  --project /work/my-project --json

# 项目路径改变：目录映射同时适用于其子目录
python3 -m migrate_agent migrate \
  --project /old/work/my-project \
  --project-map /old/work/my-project=/new/work/my-project

# 筛选指定会话，或忽略归档历史/技能
python3 -m migrate_agent migrate --session CODEX_SESSION_ID --no-archived
python3 -m migrate_agent migrate --no-skills

# 关闭从历史自动发现项目，只处理 --project 明确指定的项目
python3 -m migrate_agent migrate --no-discover-projects

# 若 Harness 的 persistence-jsonl 压缩配置是 none，需要使用同样的设置
python3 -m migrate_agent migrate --compression none

# 非托管同名文件内容不同时，默认中止；检查预览后可备份再覆盖
python3 -m migrate_agent migrate --on-conflict overwrite

# 保留冲突文件，同时迁移其余文件；报告中列出跳过项
python3 -m migrate_agent migrate --on-conflict skip

# 存在任何读取或兼容性警告时停止，不写入目标
python3 -m migrate_agent migrate --strict

# 使用迁移结果中给出的 manifest_path 回滚
python3 -m migrate_agent rollback /path/to/.dsh/.migrate-agent/runs/RUN_ID/manifest.json
```

`--from codex --to deepseek` 是默认方向；目标别名 `deepseekharness`、`deepseek-harness`、`dsh` 也可使用。`--project`、`--project-map`、`--session` 均可重复指定。`--dry-run` 等同于 `plan`，`--json` 输出不包含会话正文的结构化报告。

`--project` 的源路径用于筛选历史和读取仍存在的项目规则；没有指定时，会从本次迁移会话的现存工作目录发现项目：优先使用最近的 `.git` 根目录，无 Git 标记时仅接受工作目录自身有 agent 规则或技能的项目。不会隐式扫描文件系统根、用户主目录或包含源 Codex 数据目录的目录。`--no-project-docs` 同时关闭项目文档读取与项目规则/技能安装。已迁走的源项目目录不存在时，会话仍可按历史路径筛选，但需从备份恢复项目 Markdown。

## 迁移内容

| 来源 | 处理方式 |
| --- | --- |
| `sessions/`、`archived_sessions/` 下的 JSONL / JSONL.zst | 读取完整记录，转换为 Harness 原生会话 |
| `state_*.sqlite`、`session_index.jsonl` | 补充会话标题、项目、时间和归档信息 |
| `thread_history_*.sqlite` | 原始 rollout 不可读或不完整时，使用可见历史补充并报告降级 |
| 用户与助手消息、工具输入输出、公开推理摘要、压缩摘要 | 保留为对话或标记清楚的历史参考资料 |
| 全局 `AGENTS.md`、`AGENTS.override.md`、`instructions.md` | 导入 Harness 规则及迁移资料索引，保留目标已有文字 |
| `config.toml` 中的用户指令与允许范围内的自定义指令文件 | 转换为指令文档，保留全局或项目作用域；不导入权限和认证设置 |
| `memories/`、`memory/` 下的 Markdown | 保存副本并加入资料索引 |
| `~/.codex/skills`、共享 `~/.agents/skills` 中的自定义技能 | 迁移技能和支持文件，二进制支持文件使用原始字节写出 |
| 项目各级 `AGENTS.md`、`AGENTS.override.md` | 生成对应目录的原生 `AGENTS.md`，保留目录作用域并转换 override 优先级，新会话自动加载 |
| 项目 `.codex/config.toml` 中的用户指令 | 合并到相应目录的原生规则托管区域 |
| 项目 `.codex/skills`、`.agents/skills` | 安装为项目 `.dsh/skills` 下的原生技能，保留脚本、引用与二进制资源 |
| 项目 `.codex` 下的其他 Markdown | 保留副本和资料索引，向相关导入会话提供引用 |

原始结构化会话记录、文档与允许迁移的配置摘要会额外保存为版本化的 interchange bundle，位于目标 `.migrate-agent/bundles/`。它保留跨客户端暂时无法表示的信息，也为未来新增适配器提供统一输入。

有明确的兼容性边界：

- 历史工具动作保存为参考文本，不恢复正在运行的终端、未完成工具调用、MCP 连接或授权状态。
- Codex 的加密推理、原始流式时序、KV cache 和 UI 归档状态不能直接恢复到 Harness；归档来源会保留在迁移信息里。
- 会话中的图片和文件目前保留文本引用；嵌入式内容保留在原始记录中，本地附件文件不自动搬运，不会转换成 Harness 原生多模态消息。
- 不迁移 `auth.json`、API Key、提供商凭据、可执行 hooks、插件安装缓存或内置 `.system` 技能。模型偏好只存档，不修改 Harness 的模型设置。
- 项目代码、Git 仓库、worktree 与普通仓库文件不由本工具搬运。项目原生规则和 `.dsh/skills` 会写入原目录或 `--project-map` 指定的目标目录。
- 当前 bundle 在内存中构建；数 GB 的历史建议按 `--project` 或 `--session` 分批处理。适配器与引擎边界已分离，后续可以在读取侧增加流式批次。

## 数据写入与恢复

每个 Codex 会话映射到稳定的 Harness UUID。同样的源内容再次迁移，会识别为未变化；已有不同内容默认视为冲突。规则文件使用带来源标记的托管区域，可以在默认策略下安全合并并自动备份；`--on-conflict skip` 仍会跳过不同内容的已有文件。其他冲突通过 `--on-conflict overwrite` 选择备份覆盖。

非空 `AGENTS.override.md` 是该目录的有效源规则，被忽略的 base 不作为有效规则导出。原地迁移时会备份并替换这个失效 base，避免 Harness 同时加载它；映射到其他目录时，无关的目标现有指令会保留。嵌套目录分别生成原生规则。重复读取会剔除本工具自己的项目规则区域，并忽略生成的 `.dsh` 目录，避免反复导入输出造成内容膨胀。

项目技能会规范化 Harness 要求的名称和调用开关，保留技能正文、相对资源路径与脚本执行能力；文件仅保留当前用户需要的读写/执行权限。显式禁用模型调用的技能保持禁用，仍按其原有用户调用策略工作。同名技能的处理会列入警告，原始文件保留在迁移资料与 bundle 中。

修改前创建备份和 manifest，文件使用临时文件加原子替换写入。失败会尝试自动恢复，并保留恢复日志。回滚会先验证目标文件和备份校验和，若会话在 Harness 中继续使用后内容已变化，会拒绝覆盖。进程异常退出留下的锁位于 `.migrate-agent/migration.lock`，确认没有迁移进程后可手动清除，再用 manifest 恢复。

对于带 WAL 的 SQLite，读取发生在临时隔离快照中，避免 SQLite 的只读连接也更新源 SHM 文件。临时空间需要能容纳相应数据库及 WAL；快照完成后删除。源文件持续变化导致三次快照都不稳定时，会报告该项无法读取，适合停止原客户端写入后重试。

迁移完全在本机完成；对话、文档和技能副本可能含有你原有的敏感内容，应按原始数据的访问级别保管。报告不输出正文，迁移生成文件默认仅当前用户可读写。

## 扩展与验证

架构与接入方式见 [`docs/architecture.md`](docs/architecture.md)。源适配器负责读取，目标适配器负责生成写入计划，统一引擎负责冲突、备份、事务与回滚；新增 agent 不需要为已有每个 agent 编写一组转换器。

```bash
python3 -B -m unittest discover -v
python3 -B -m migrate_agent adapters --json
```

测试覆盖真实格式的模拟数据、压缩历史、SQLite 回退、筛选、规则和技能、原生会话结构、去重、冲突、回滚与源数据保护。开发时另外使用本机安装包的原生 Session v4 解码器进行恢复校验；详见架构文档。

项目生效验证使用官方原生规则加载器、技能文件系统 provider 和技能 registry，在没有导入历史的全新空会话中检查根目录/子目录规则、兄弟目录隔离、技能目录及正文、相对资源和可执行脚本。安装包的依赖目录可用时可重跑：

```bash
MIGRATE_AGENT_DSH_NODE_MODULES=/path/to/harness/node_modules \
  python3 -B -m unittest tests.test_native_project -v
```

按 Harness 官方默认方式，项目根由最近的 `.git` 标记确定；没有 Git 标记时根就是会话工作目录，请在目标项目根目录新建会话。技能目标若只是另一个 Git 仓库的子目录，预览会明确拒绝，要求选择实际仓库根或映射到独立项目，避免报告安装成功却无法发现。规则和技能加载插件需启用（标准配置默认启用）；规则也受客户端配置的字节预算约束。[官方规则说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/context/agent-instructions/README.md) · [官方技能说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/skill/skill-filesystem/README.md)
