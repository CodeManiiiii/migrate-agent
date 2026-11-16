# migrate-agent

将 coding agent 的会话、上下文、规则和技能迁移到新客户端。目前支持 **Codex → DeepSeek Harness**，采用可扩展的适配器架构，便于后续接入其他 agent。

导入的历史可以继续对话，项目规则和技能可供 Harness 新建会话自动发现。

## 使用

需要 Python 3.9 或更新版本：

```bash
git clone https://github.com/CodeManiiiii/migrate-agent.git
cd migrate-agent
python3 -m pip install -e '.[codex]'

migrate-agent plan     # 预览
migrate-agent migrate  # 执行迁移
```

完成后重启 Harness 查看历史；在对应项目根目录新建会话即可加载规则和技能。

Codex 数据目录保持只读；项目规则和技能会生成或更新，修改均有备份，支持回滚。

## 文档

- [快速上手](USAGE.md)：安装、只读验证和回滚。
- [详细使用说明](docs/guide.md)：项目筛选、路径映射、迁移范围和兼容性。
- [架构与开发](docs/architecture.md)：适配器设计、扩展方式和测试。
