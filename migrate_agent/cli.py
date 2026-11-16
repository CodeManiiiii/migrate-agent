"""Single-command migration and read-only preview."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .models import SourceOptions, TargetOptions


def _path(value: str) -> Path:
    return Path(value).expanduser().absolute()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate-agent", description="迁移 coding agent 的会话、规则、记忆和技能。"
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("adapters", help="列出可用的源/目标适配器").add_argument("--json", action="store_true")
    for name, help_text in [("plan", "预览迁移，不写入任何文件"), ("migrate", "执行一次完整迁移")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--from", dest="source", default="codex", help="源 agent（默认 codex）")
        command.add_argument("--to", dest="target", default="deepseek", help="目标 agent（默认 deepseek）")
        command.add_argument("--source-home", help="Codex 数据目录（默认 CODEX_HOME 或 ~/.codex）")
        command.add_argument("--target-home", help="Harness 数据目录（默认 DSH_HOME 或 ~/.dsh）")
        command.add_argument("--compression", choices=["zstd", "none"], default="zstd", help="匹配 Harness 的会话压缩设置（默认 zstd）")
        command.add_argument("--project", action="append", default=[], help="筛选项目并扫描其 Markdown，可多次指定")
        command.add_argument("--project-map", action="append", default=[], metavar="OLD=NEW", help="重映射会话的项目路径，可多次指定")
        command.add_argument("--session", action="append", default=[], help="只迁移指定会话 ID，可多次指定")
        command.add_argument("--no-archived", action="store_true", help="不迁移已归档会话")
        command.add_argument("--no-skills", action="store_true", help="不迁移技能")
        command.add_argument("--no-project-docs", action="store_true", help="不扫描项目 Markdown")
        command.add_argument("--no-discover-projects", action="store_true", help="只扫描 --project 明确指定的项目，不根据历史自动发现")
        command.add_argument("--on-conflict", choices=["abort", "skip", "overwrite"], default="abort", help="同名不同内容：中止、跳过或备份后覆盖（默认中止）")
        command.add_argument("--strict", action="store_true", help="存在适配警告时中止")
        command.add_argument("--json", action="store_true", help="以 JSON 输出迁移报告，不输出会话正文")
        command.add_argument("--dry-run", action="store_true", help="仅预览，等同 plan")
    rollback = commands.add_parser("rollback", help="根据 manifest 回滚；目标被手动修改时拒绝覆盖")
    rollback.add_argument("manifest", type=_path)
    rollback.add_argument("--json", action="store_true")
    return parser


def _options(args: argparse.Namespace) -> tuple:
    source_home = _path(args.source_home or os.environ.get("CODEX_HOME", "~/.codex"))
    target_home = _path(args.target_home or os.environ.get("DSH_HOME", "~/.dsh"))
    mappings = {}
    for mapping in args.project_map:
        if "=" not in mapping:
            raise ValueError("--project-map 必须使用 OLD=NEW 格式")
        before, after = mapping.split("=", 1)
        if not before or not after:
            raise ValueError("--project-map 的源路径和目标路径不能为空")
        key, value = str(_path(before)), str(_path(after))
        if key in mappings and mappings[key] != value:
            raise ValueError("同一个项目存在不同的 --project-map 目标：" + key)
        mappings[key] = value
    if source_home == target_home or source_home in target_home.parents or target_home in source_home.parents:
        raise ValueError("源目录和目标目录必须相互独立")
    return (
        SourceOptions(
            home=source_home,
            projects=tuple(_path(value) for value in args.project),
            session_ids=tuple(args.session),
            include_archived=not args.no_archived,
            include_skills=not args.no_skills,
            include_project_docs=not args.no_project_docs,
            discover_projects=not args.no_discover_projects,
        ),
        TargetOptions(home=target_home, project_map=mappings, compression=args.compression),
    )


def _print_report(report: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print("迁移报告")
    # Keep console output short even when thousands of native session files exist.
    for key in ("status", "source", "target_home", "sessions", "documents", "summary", "manifest_path"):
        if key in report:
            value = report[key]
            print("  {}: {}".format(key, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value))
    warnings = report.get("warnings", [])
    for warning in warnings:
        print("  警告: " + str(warning))
    for instruction in report.get("instructions", []):
        print("  " + str(instruction))
    if "files" in report:
        changed = [entry for entry in report["files"] if entry.get("action") not in ("unchanged", "skip")]
        for entry in changed[:12]:
            print("  {} {}".format(entry.get("action", ""), entry.get("destination", entry.get("path", ""))))
        if len(changed) > 12:
            print("  另有 {} 项；使用 --json 查看完整计划。".format(len(changed) - 12))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        from .engine import MigrationEngine
        from .registry import default_registry
        from .service import MigrationService

        if args.command == "adapters":
            report = default_registry().describe()
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print("源: " + ", ".join(report["sources"]))
                print("目标: " + ", ".join(report["targets"]))
            return 0
        if args.command == "rollback":
            report = MigrationEngine().rollback(args.manifest)
        else:
            source_options, target_options = _options(args)
            service = MigrationService()
            plan = service.prepare(args.source, args.target, source_options, target_options, conflict=args.on_conflict, strict=args.strict)
            if args.command == "plan" or args.dry_run:
                report = plan.to_dict()
                report["status"] = "preview"
            else:
                report = service.engine.apply(plan)
        _print_report(report, args.json)
        return 0
    except KeyboardInterrupt:
        print("操作已中断；若产生 manifest，可检查报告后回滚。", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        else:
            print("迁移失败: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
