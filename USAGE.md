# 快速上手

将 Codex 的会话、上下文、规则和技能迁移到 DeepSeek Harness。

## 1. 安装

需要 Python 3.9 或更新版本：

```bash
git clone https://github.com/CodeManiiiii/migrate-agent.git
cd migrate-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[codex]'
```

Windows 将激活命令换成 `.venv\Scripts\activate`。

## 2. 预览并迁移

```bash
# 先预览，不写入文件
migrate-agent plan

# 确认预览后执行迁移
migrate-agent migrate
```

只迁移某个项目时，给以上命令加上 `--project /项目绝对路径`。macOS 安装后也可双击 `migrate.command` 执行迁移。

默认读取本机 Codex 数据目录并写入 Harness。**Codex 数据目录保持只读**；项目中的 `AGENTS.md` 和 `.dsh/skills` 会生成或更新，修改均有备份。

## 3. 打开 Harness

重启 DeepSeek Harness 后查看导入的历史，会话可能先出现在“未分组”。在对应项目根目录新建会话，项目规则和技能即可自动发现。

验证时选择“仅可查看”，例如询问：“请只读列出当前项目规则和可用技能，不修改文件。”

## 需要撤销时

将路径替换为迁移报告中的 `manifest_path`：

```bash
migrate-agent rollback /path/to/manifest.json
```

更多选项、项目路径映射和兼容性说明见 [详细使用说明](docs/guide.md)。
