# migrate-agent 使用说明

把 Codex 的会话、上下文、项目规则、记忆文档和自定义技能迁移到 DeepSeek Harness。当前支持的方向是 **Codex → DeepSeek Harness**。

## 1. 安装

需要 Python 3.9 或更高版本，建议使用虚拟环境：

```bash
git clone https://github.com/CodeManiiiii/migrate-agent.git
cd migrate-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[codex]'
```

Windows 激活环境时使用 `.venv\Scripts\activate`。后续的 `python` 指已激活环境中的解释器。

安装后的命令为 `migrate-agent`；在源码目录也可运行 `python -m migrate_agent`。`[codex]` 提供压缩历史解码和旧版 Python 的 TOML 解析依赖。

```bash
migrate-agent --version
migrate-agent adapters
```

迁移过程在本机完成，不需要模型 API Key。与 Harness 模型进行对话验证时，使用其现有账号和模型配置。

## 2. 先预览

```bash
migrate-agent plan
migrate-agent plan --json > migration-plan.json
```

默认方向是 `--from codex --to deepseek`，默认读取 `CODEX_HOME` 或 `~/.codex`，写入 `DSH_HOME` 或 `~/.dsh`。

预览读取源数据、检查目标文件，不写入 Harness 或项目目录。报告列出会话、文档、项目输出目录，以及每个文件的动作：新增、更新、未变化、跳过或冲突，并提供兼容性警告和会话 ID 映射。

JSON 报告不输出会话正文，但包含本机路径和会话标识，分享前请检查。

## 3. 一键迁移

```bash
migrate-agent migrate
```

未指定项目时，会从本次选中历史的现存工作目录自动发现项目，优先使用最近的 `.git` 根。无 Git 标记时，仅接受工作目录自身存在 agent 规则或技能的项目；不会隐式扫描文件系统根和用户主目录。

macOS 也可在安装依赖后双击 `migrate.command`；需要特定 Python 时，可将 `MIGRATE_AGENT_PYTHON` 设置为其可执行文件路径。

**写入范围：** Codex 数据目录保持只读。默认会在项目中生成或合并 `AGENTS.md`，并创建 `.dsh/skills`，修改均纳入备份和回滚。若原项目文件也需要保持不变，请使用第 5 节的项目路径映射。

完成后请保留报告中的 `manifest_path`，它是这次迁移的恢复入口。

## 4. 筛选范围

```bash
# 只处理这个项目及子目录中的会话，读取其规则和技能
migrate-agent plan --project /path/to/project
migrate-agent migrate --project /path/to/project

# 只处理指定会话
migrate-agent migrate --session CODEX_SESSION_ID

# 组合筛选
migrate-agent migrate \
  --project /path/to/project \
  --session CODEX_SESSION_ID
```

`--project`、`--session` 均可重复指定。建议选择实际仓库根。仅指定的会话都不可读时，会报错而不导入无关文档。

```bash
# 不迁移归档会话
migrate-agent migrate --no-archived

# 不迁移技能
migrate-agent migrate --no-skills

# 不读取或安装项目规则、项目技能和项目 Markdown
migrate-agent migrate --no-project-docs

# 不自动发现项目，仅扫描 --project 明确指定的项目
migrate-agent migrate --no-discover-projects
```

## 5. 项目换位置，或保留原项目不变

先准备目标项目目录，再映射路径：

```bash
migrate-agent plan \
  --project /old/project \
  --project-map /old/project=/new/project

migrate-agent migrate \
  --project /old/project \
  --project-map /old/project=/new/project
```

会话工作目录和规则、技能写入位置均转到 `/new/project`，原项目不写入。映射适用于子目录；多个映射同时匹配时使用最具体的路径。

本工具不搬运代码、Git 仓库或 worktree。换电脑时先另行准备项目代码，再迁移上下文。无 Git 标记的项目应在目标根目录新建 Harness 会话；若技能目标只是其他 Git 仓库的子目录，会在预览阶段报错，避免生成客户端无法发现的文件。

## 6. 指定数据目录和编码

```bash
migrate-agent migrate \
  --source-home /backup/codex \
  --target-home /data/deepseek \
  --project /path/to/project
```

源和目标目录必须相互独立。目标不能设为 Codex 数据目录或其子目录。

`--compression` 应与 Harness 的 `session-persistence-jsonl` 配置保持一致：

```bash
# Harness 默认值
migrate-agent migrate --compression zstd

# 仅当目标客户端配置为 none 时使用
migrate-agent migrate --compression none
```

同一个已迁移会话目录不允许混用两种编码。需要改变编码时，先按 manifest 回滚，或使用独立目标目录。

## 7. 在 Harness 中验证

1. 打开或重新启动 Harness，刷新外部加入的历史。
2. 检查“未分组”。已有工作区的成员关系单独管理，导入历史不一定直接进入原工作区。
3. 打开导入历史；缺少缓存时可能先显示“未命名”，打开后可恢复原始标题。
4. 在对应目标项目根新建会话，选择“仅可查看”模式。
5. 发送简单只读问题，确认自动上下文和技能加载。

规则验证示例：

> 仅根据本会话已自动加载的 AGENTS.md，概括两条约定或近期工作内容，并说明来源。不要另外搜索文件，不要修改文件或运行命令。

技能验证示例，将 `example-skill` 换成迁移后的实际名称：

> 请仅调用 skill 工具读取 example-skill，用两句话说明其用途和来源目录。不要执行技能工作流，不要修改文件，不要运行命令或脚本。

AGENTS.md 可能记录历史记忆而非技术规范，应按实际内容验证。显式禁止模型调用的技能保持原有限制。规则和技能加载插件需启用，标准配置默认包含相应能力。

## 8. 冲突与重复迁移

源内容不变时会识别未变化文件，不会生成重复的目标会话 ID。

```bash
# 普通同名文件内容不同时，默认中止
migrate-agent migrate --on-conflict abort

# 保留已有冲突文件，继续迁移其余内容
migrate-agent migrate --on-conflict skip

# 备份后覆盖普通冲突文件
migrate-agent migrate --on-conflict overwrite
```

规则托管区域可在默认策略下合并并备份，无关目标指令会保留。同级非空 `AGENTS.override.md` 的替代含义也会转换。

分别运行 `plan` 和 `migrate` 时会重新读取源数据；执行时还会检查本次计划生成后目标是否变化，先前报告不是冻结后的可执行计划。

## 9. 回滚

使用执行报告返回的实际路径：

```bash
migrate-agent rollback /path/to/.dsh/.migrate-agent/runs/RUN_ID/manifest.json
```

回滚校验目标内容、权限和备份。迁移后已被 Harness 或用户修改的文件会受到保护，工具将拒绝覆盖。多次相关迁移应按时间倒序回滚。

异常中断可能留下目标 `.migrate-agent/migration.lock`。确认没有迁移进程运行后，再清除遗留锁并通过 manifest 恢复。

## 10. 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 缺少解压器或 TOML 解析器 | 使用 `python -m pip install -e '.[codex]'` 安装完整依赖 |
| SQLite 快照持续变化 | 暂停源客户端写入后重试；WAL 读取使用临时快照，不修改源 SHM |
| 导入历史未显示 | 重启客户端、检查“未分组”，确认压缩配置和目标数据目录 |
| 技能未发现 | 检查实际项目根、最终技能名称、插件和调用策略 |
| 规则截断 | 检查 Harness 规则字节预算；超过默认预算会警告，超大单文件会拒绝 |
| 大历史占用较多内存 | 按 `--project` 或 `--session` 分批；当前 bundle 在内存中构建 |

## 11. 兼容性和数据边界

- 图片和文件附件保留文本引用及原始嵌入记录，不转换为 Harness 原生多模态消息。
- 运行中的终端、未完成工具状态、加密推理、KV cache 和源端 UI 归档状态不直接恢复。
- 历史工具调用作为参考文本保存，不会重放。
- 不迁移认证文件、API Key、可执行 hooks、插件缓存和内置系统技能。
- 源记录与允许迁移的文档存入目标 `.migrate-agent/bundles`，应按原数据的访问级别保管。真实测试数据不应提交到版本库。

## 12. 开发验证

```bash
python -B -m unittest discover -v
```

普通测试不需要运行 Harness。可选原生测试需要可用的 Harness 依赖目录和支持相关功能的 Node，详见[架构设计与验证说明](docs/architecture.md)。

Source / Target 适配器通过统一 Bundle 扩展迁移方向；未验证的其他 coding agent 尚未列为已支持。
