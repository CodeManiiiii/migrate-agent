"""DeepSeek Harness 0.2.2 / Session v4 native writer.

The target adapter is pure planning: only the migration engine writes files.
Historical actions are recall text, never executable target tool calls.
"""

import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import uuid

from ..models import PlannedFile, TargetPlan

FORMAT_VERSION = 4
TESTED_DSH_VERSION = "0.2.2-rc.3"


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def zstd_raw_frame(data):
    """Encode a standard Zstandard frame with raw blocks (RFC 8878).

    Raw blocks need no compression library. The result is accepted by the native
    Harness decoder; its size is slightly larger than plaintext. Header and body
    must be separate frames because Harness reads the header independently.
    """
    size = len(data)
    if size < 256:
        header = b"\x20" + size.to_bytes(1, "little")
    elif size < 65792:
        header = b"\x60" + (size - 256).to_bytes(2, "little")
    elif size <= 0xFFFFFFFF:
        header = b"\xa0" + size.to_bytes(4, "little")
    else:
        header = b"\xe0" + size.to_bytes(8, "little")
    result = bytearray(b"\x28\xb5\x2f\xfd" + header)
    # Raw blocks are limited to 128 KiB, including in single-segment frames.
    chunks = [data[i:i + 131072] for i in range(0, size, 131072)] or [b""]
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        result.extend(((len(chunk) << 3) | int(last)).to_bytes(3, "little"))
        result.extend(chunk)
    return bytes(result)


def _utf16_units(value):
    data = value.encode("utf-16-le", errors="surrogatepass")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def encode_segment(value):
    """Mirror Harness encodeSegment(), including UTF-16 surrogate handling."""
    if not value:
        raise ValueError("Empty DeepSeek path segment")
    if value in (".", ".."):
        return "~002E" * len(value)
    return "".join(chr(code) if (48 <= code <= 57 or 65 <= code <= 90 or
                                   97 <= code <= 122 or code in (46, 95, 45))
                   else "~%04X" % code for code in _utf16_units(value))


def project_key(cwd):
    """Mirror the native, human-readable per-project directory key."""
    if not cwd:
        raise ValueError("Empty project cwd")
    parts = []
    separator_run = False
    for code in _utf16_units(cwd):
        if code in (47, 92, 58):
            if not separator_run:
                parts.append("-")
            separator_run = True
        else:
            # projectKey encodes code units directly. encodeSegment('.') has
            # a whole-segment traversal guard that must not be applied to an
            # embedded period (for example ".real-migration" or "a.b").
            parts.append(chr(code) if (48 <= code <= 57 or 65 <= code <= 90 or
                                      97 <= code <= 122 or code in (46, 95, 45))
                         else "~%04X" % code)
            separator_run = False
    slug = "".join(parts).lstrip("-") or "root"
    return "--" + slug[:251] + "--"


def _safe_relative(value):
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid document path: %r" % value)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".") for part in value.split("/")):
        raise ValueError("Unsafe document path: %r" % value)
    return str(path)


def _millis(value, fallback=0):
    if not value:
        return fallback
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return max(0, int(instant.timestamp() * 1000))
    except (ValueError, TypeError, OverflowError):
        return fallback


def _mapped_cwd(cwd, mappings):
    # A root mapping applies to descendant projects as well.
    for source, target in sorted(mappings.items(), key=lambda pair: len(pair[0]), reverse=True):
        try:
            relative = Path(cwd).expanduser().resolve().relative_to(Path(source).expanduser().resolve())
        except ValueError:
            continue
        return str(Path(target) / relative)
    return cwd


def _document_bytes(document):
    if document.encoding == "base64":
        return base64.b64decode(document.content, validate=True)
    if document.encoding != "utf-8":
        raise ValueError("Unsupported document encoding: " + document.encoding)
    return document.content.encode("utf-8")


def _recall_text(message, source):
    details = ["历史记录", "role=" + message.role, "kind=" + message.kind]
    if message.name:
        details.append("tool=" + message.name)
    if message.call_id:
        details.append("call_id=" + message.call_id)
    return "[" + source + " " + ", ".join(details) + "; 仅供上下文参考，动作已执行，不要自动重放]\n" + message.content


def native_session(session, cwd, context, compression="zstd", source="codex"):
    """Return a deterministic native v4 artifact and its stable target id."""
    target_id = "session-" + str(uuid.uuid5(uuid.NAMESPACE_URL, "migrate-agent:" + source + ":" + session.id))
    created = _millis(session.created_at)
    header = {"type": "session", "version": FORMAT_VERSION, "id": target_id,
              "createdAt": created, "isSeeded": False, "delegationDepth": 0}
    if cwd:
        header["cwd"] = cwd
    events = []
    clock = created
    turn = 1
    step = 0

    def append(kind, data, surface=False):
        event = {"type": kind, "seq": len(events), "time": clock, "data": data}
        if surface:
            event["surfaceOp"] = "append"
        events.append(event)

    def user(text, identity, human=False):
        source = {"kind": "user"} if human else {"kind": "agent-migration", "form": "recall"}
        append("user/message", {"id": identity, "role": "user", "source": source,
                                "content": [{"type": "text", "text": text}]}, True)

    append("turn/start", {"turn": turn})
    step += 1
    append("step/start", {"turn": turn, "step": step})
    user(context, target_id + "-context")
    append("step/end", {"turn": turn, "step": step})
    human_seen = False
    for index, message in enumerate(session.messages):
        clock = max(clock, _millis(message.timestamp, clock))
        human = message.role == "user" and message.kind == "message"
        assistant = message.role == "assistant" and message.kind in ("message", "agent_message")
        if human and human_seen:
            append("turn/end", {"turn": turn, "reason": {"kind": "completed"}})
            turn += 1
            step = 0
            append("turn/start", {"turn": turn})
        human_seen = human_seen or human
        step += 1
        append("step/start", {"turn": turn, "step": step})
        identity = target_id + "-message-" + str(index)
        if assistant:
            model = str(message.metadata.get("model") or session.metadata.get("model") or source + "-imported")
            value = {"id": identity, "role": "assistant",
                     "source": {"kind": "model", "provider": source, "model": model},
                     "content": [{"type": "text", "text": message.content}]}
            # Source streaming chunk timing is unavailable. A single reconstructed
            # chunk is explicitly an import representation, not original telemetry.
            stream = [{"type": "text-chunks", "time0": clock, "index": 0,
                       "dt": [], "texts": [message.content]}]
            append("assistant/message", {"turn": turn, "step": step,
                                         "message": value, "stream": stream}, True)
        else:
            user(message.content if human else _recall_text(message, source), identity, human)
        append("step/end", {"turn": turn, "step": step})
    append("turn/end", {"turn": turn, "reason": {"kind": "completed"}})
    append("session/title", {"title": session.title or "从 " + source + " 迁移的会话",
                             "messageSeqs": [], "source": {"kind": "user"}})
    head = _json(header) + b"\n"
    body = b"\n".join(_json(event) for event in events) + b"\n"
    if compression == "none":
        return target_id, head + body
    if compression != "zstd":
        raise ValueError("Unsupported DeepSeek compression: " + str(compression))
    return target_id, zstd_raw_frame(head) + zstd_raw_frame(body)


def _merge_instructions(existing, section, source):
    marker_start = "<!-- migrate-agent:" + encode_segment(source) + ":start -->"
    marker_end = "<!-- migrate-agent:" + encode_segment(source) + ":end -->"
    start = existing.find(marker_start)
    end = existing.find(marker_end)
    if start != -1 and end >= start:
        existing = existing[:start] + existing[end + len(marker_end):]
    elif start != -1 or end != -1:
        raise ValueError("Target AGENTS.md contains an incomplete migrate-agent managed section")
    before = existing.rstrip()
    return (before + ("\n\n" if before else "") + marker_start + "\n" +
            section.rstrip() + "\n" + marker_end + "\n").encode("utf-8")


def _without_managed(text, source):
    start_marker = "<!-- migrate-agent:" + encode_segment(source) + ":start -->"
    end_marker = "<!-- migrate-agent:" + encode_segment(source) + ":end -->"
    start, end = text.find(start_marker), text.find(end_marker)
    if start == -1 and end == -1:
        return text
    if start == -1 or end < start or text.find(start_marker, start + 1) != -1:
        raise ValueError("Project AGENTS.md contains an invalid migrate-agent managed section")
    return (text[:start] + text[end + len(end_marker):]).rstrip()


def _project_root(plan, directory):
    root = Path(directory).expanduser().absolute()
    identity = "project:" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:20]
    plan.project_roots[identity] = root
    return identity, root


def _project_rule_files(bundle, options, plan):
    """Create native workspace files, independent of migrated session history."""
    groups = {}
    for document in bundle.documents:
        if document.scope != "project" or document.kind != "instruction":
            continue
        if document.encoding != "utf-8":
            raise ValueError("Project instruction is not UTF-8: " + document.path)
        relative = PurePosixPath(_safe_relative(document.path))
        root = Path(_mapped_cwd(document.project, options.project_map)).expanduser().absolute()
        directory = root / Path(*relative.parent.parts)
        group = groups.setdefault(str(directory), {"roots": set(), "documents": {}})
        group["roots"].add(root)
        key = (str(Path(document.project).resolve()), document.path)
        group["documents"][key] = document
    generated = []
    for directory, group in sorted(groups.items()):
        directory = Path(directory)
        root = min(group["roots"], key=lambda path: (len(path.parts), str(path)))
        root_id, root = _project_root(plan, root)
        relative = (directory.relative_to(root) / "AGENTS.md").as_posix()
        target = directory / "AGENTS.md"
        if target.is_symlink():
            raise ValueError("Refusing symlink project instruction: " + str(target))
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        unmanaged = _without_managed(existing, bundle.source)
        docs = sorted(group["documents"].values(), key=lambda doc: doc.path)
        bases = [doc for doc in docs if PurePosixPath(doc.path).name == "AGENTS.md"]
        overrides = [doc for doc in docs if PurePosixPath(doc.path).name == "AGENTS.override.md" and doc.content.strip()]
        extras = [doc for doc in docs if PurePosixPath(doc.path).name not in {"AGENTS.md", "AGENTS.override.md"}]
        base = _without_managed(bases[-1].content, bundle.source) if bases else ""
        sections = []
        if overrides:
            # Codex ignores the base file when an override exists. If this is
            # an in-place conversion, retire only that exact ignored base;
            # unrelated target instructions remain intact and are backed up.
            if not base:
                source_base = Path(overrides[-1].project) / PurePosixPath(overrides[-1].path).parent / "AGENTS.md"
                if source_base.is_file() and not source_base.is_symlink():
                    base = _without_managed(source_base.read_text(encoding="utf-8"), bundle.source)
            if base and unmanaged.strip() == base.strip():
                unmanaged = ""
            effective = overrides[-1]
            sections.append("### " + bundle.source + " 项目覆盖规则（替代源目录的 AGENTS.md）\n\n" + effective.content)
        elif base:
            if not unmanaged.strip():
                unmanaged = base
            elif unmanaged.strip() != base.strip():
                sections.append("### " + bundle.source + " 项目规则\n\n" + base)
        for document in extras:
            sections.append("### " + bundle.source + " 项目用户指令：" + document.path + "\n\n" + document.content)
        if sections:
            content = _merge_instructions(unmanaged, "\n\n".join(sections), bundle.source)
        else:
            # Keep a directly compatible source AGENTS.md byte-for-byte when
            # no conversion or managed additions are necessary.
            content = unmanaged.encode("utf-8")
            if not bases and not content:
                continue
        if len(content) > 1_048_576:
            raise ValueError("Project AGENTS.md exceeds Harness's default 1 MiB source limit: " + str(target))
        if len(content) > 65_536:
            plan.warnings.append("项目规则 " + str(target) + " 超过 Harness 默认 65536 字节指令链预算，需提高 agent-instructions.maxBytes 后才能完整加载。")
        plan.files.append(PlannedFile(relative, content, "Native project workspace instructions", root=root_id, merge=True))
        generated.append((directory, len(content)))
    for directory, _ in generated:
        chain_bytes = sum(size for ancestor, size in generated if ancestor == directory or ancestor in directory.parents)
        if chain_bytes > 65_536:
            message = "项目 " + str(directory) + " 的规则链超过 Harness 默认 65536 字节预算，需提高 agent-instructions.maxBytes 后才能完整加载。"
            if message not in plan.warnings:
                plan.warnings.append(message)


def _yaml_scalar(block):
    """Read the scalar subset used by portable skill routing frontmatter."""
    if block is None:
        return None
    first, *tail = block.splitlines()
    value = first.split(":", 1)[1].strip()
    if value.startswith(('"', "'")):
        if value.startswith('"'):
            try:
                return json.JSONDecoder().raw_decode(value)[0]
            except (ValueError, TypeError):
                raise ValueError("Unsupported quoted skill frontmatter scalar")
        match = re.match(r"^'((?:[^']|'')*)'", value)
        if match:
            return match.group(1).replace("''", "'")
        raise ValueError("Invalid quoted skill frontmatter scalar")
    if value[:1] in {"|", ">"}:
        lines = [line.strip() for line in tail]
        return ("\n" if value.startswith("|") else " ").join(lines).strip()
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _skill_parts(text):
    lines = text.lstrip("\ufeff").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, [], text
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") in {"---", "..."}), None)
    if end is None:
        raise ValueError("Skill YAML frontmatter has no closing delimiter")
    fields, order = {}, []
    current = None
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*|\"[^\"]+\"|'[^']+')\s*:", line)
        if match:
            key = match.group(1).strip('"\'')
            if key in fields:
                raise ValueError("Duplicate skill frontmatter key: " + key)
            current = key
            fields[key] = line
            order.append(key)
        elif current is not None:
            fields[current] += line
        elif line.strip() and not line.lstrip().startswith("#"):
            raise ValueError("Unsupported skill frontmatter mapping")
    return fields, order, "".join(lines[end + 1:])


def _skill_bool(value, field):
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    raise ValueError("Invalid skill invocation field " + field)


def _skill_slug(value):
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    slug = re.sub(r"[^a-z0-9]+", "-", separated.lower()).strip("-")
    return slug or "skill-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _native_skill(content, fallback_name, warnings, rename=None):
    text = content.decode("utf-8")
    fields, order, body = _skill_parts(text)
    original_name = _yaml_scalar(fields.get("name"))
    name = rename or _skill_slug(str(original_name or fallback_name))
    description = _yaml_scalar(fields.get("description"))
    if not isinstance(description, str) or not description.strip():
        description = "从 coding agent 迁移的项目技能 " + name
        warnings.append("项目技能 " + fallback_name + " 缺少 description，已补充可发现的描述；原文件保存在迁移包中。")
    if original_name != name:
        warnings.append("项目技能名称 " + str(original_name or fallback_name) + " 已转换为 Harness 支持的 " + name + "。")
    policies = {}
    aliases = {"disableModelInvocation": ("disable-model-invocation", False),
               "modelInvocable": ("disable-model-invocation", True),
               "userInvocable": ("user-invocable", False)}
    for key in ("disable-model-invocation", "user-invocable"):
        if key in fields:
            policies[key] = _skill_bool(_yaml_scalar(fields[key]), key)
    for key, (canonical, inverse) in aliases.items():
        if key not in fields:
            continue
        value = _skill_bool(_yaml_scalar(fields[key]), key)
        value = not value if inverse else value
        if canonical in policies and policies[canonical] != value:
            raise ValueError("Conflicting skill invocation fields: " + key + " / " + canonical)
        policies[canonical] = value
        warnings.append("项目技能 " + name + " 的 " + key + " 已转换为 " + canonical + "。")
    removed = {"name", "description", *aliases, "disable-model-invocation", "user-invocable"}
    header = ["---", "name: " + json.dumps(name, ensure_ascii=False), "description: " + json.dumps(description, ensure_ascii=False)]
    header.extend(key + ": " + ("true" if value else "false") for key, value in sorted(policies.items()))
    header.extend(fields[key].rstrip() for key in order if key not in removed)
    return ("\n".join(header) + "\n---\n" + body).encode("utf-8"), name


def _existing_skill_names(project):
    result = {}
    for root in (project / ".dsh" / "skills", project / ".agents" / "skills"):
        if not root.is_dir() or root.is_symlink():
            continue
        for entry in root.iterdir():
            if entry.is_symlink():
                continue
            file = entry / "SKILL.md" if entry.is_dir() else entry if entry.suffix == ".md" else None
            if file is None or file.is_symlink() or not file.is_file():
                continue
            try:
                fields, _, _ = _skill_parts(file.read_text(encoding="utf-8"))
                name = _yaml_scalar(fields.get("name"))
                if isinstance(name, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
                    result.setdefault(name, set()).add(file.absolute())
            except (OSError, ValueError, UnicodeError):
                continue
    return result


def _project_skill_files(bundle, options, plan):
    groups = {}
    entries = []
    bundle_parents = {}
    for document in bundle.documents:
        if document.scope != "project" or document.kind != "skill":
            continue
        parts = PurePosixPath(_safe_relative(document.path)).parts
        if len(parts) < 3 or parts[:2] not in {(".codex", "skills"), (".agents", "skills")}:
            plan.warnings.append("项目技能路径未识别，已保留资料副本：" + document.path)
            continue
        project = Path(_mapped_cwd(document.project, options.project_map)).expanduser().absolute()
        native_root = next((candidate for candidate in (project, *project.parents)
                            if (candidate / ".git").exists()), project)
        if native_root != project:
            raise ValueError("项目技能目标 " + str(project) + " 不是 Harness 的原生项目根；实际 Git 根为 " + str(native_root) +
                             "。请使用该仓库根作为 --project，或通过 --project-map 映射到独立的目标项目根，避免生成无法发现的技能。")
        entries.append((document, parts, project))
        if parts[-1] == "SKILL.md" and len(parts) >= 4:
            bundle_parents.setdefault((str(project), parts[0]), set()).add(parts[2:-1])
    for document, parts, project in entries:
        relative = parts[2:]
        candidates = [parent for parent in bundle_parents.get((str(project), parts[0]), ())
                      if relative[:len(parent)] == parent]
        parent = max(candidates, key=len) if candidates else relative[:1]
        # Harness discovers only direct child bundles. Flatten nested source
        # bundles, while keeping each bundle's own resource paths unchanged.
        folder = "-".join(parent)
        key = (str(project), parts[0], "/".join(parent))
        group = groups.setdefault(key, {"project": project, "files": {}, "executables": set(),
                                        "source_root": parts[0], "folder": folder, "original_parent": parent})
        child = "/".join(relative[len(parent):])
        group["files"][child] = _document_bytes(document)
        if getattr(document, "executable", False):
            group["executables"].add(child)
    occupied_names, occupied_paths, tree_contents = {}, {}, {}
    for key, group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1] != ".codex", item[0][2])):
        project = group["project"]
        project_key_value = str(project)
        if project_key_value not in occupied_names:
            occupied_names[project_key_value] = _existing_skill_names(project)
        names = occupied_names[project_key_value]
        files = dict(group["files"])
        flat = "" in files and group["folder"].endswith(".md")
        main = "" if flat else "SKILL.md" if "SKILL.md" in files else None
        folder = group["folder"]
        canonical = None
        if main is not None:
            files[main], canonical = _native_skill(files[main], PurePosixPath(folder).stem if flat else folder, plan.warnings)
        destination_folder = folder
        tree_identity = (tuple(sorted(files.items())), tuple(sorted(group["executables"])))
        relative_prefix = ".dsh/skills/" + destination_folder
        prior = tree_contents.get((project_key_value, relative_prefix))
        if prior == tree_identity:
            continue
        if prior is not None and prior != tree_identity:
            destination_folder = group["source_root"].lstrip(".") + "-" + folder
            relative_prefix = ".dsh/skills/" + destination_folder
            suffix = 2
            while (project_key_value, relative_prefix) in tree_contents:
                relative_prefix = ".dsh/skills/" + destination_folder + "-" + str(suffix)
                suffix += 1
        destination_main = project / relative_prefix / main if main else project / relative_prefix
        if canonical is not None:
            conflict_paths = names.get(canonical, set())
            # Source shared skills already discovered at their original path
            # are the very skill being translated, not unrelated collisions.
            original_path = project / group["source_root"] / "skills" / Path(*group["original_parent"])
            original_main = original_path if flat else original_path / "SKILL.md"
            conflicting = conflict_paths - {destination_main.absolute(), original_main.absolute()}
            if conflicting:
                candidate = _skill_slug(group["source_root"].lstrip(".") + "-" + canonical)
                suffix = 2
                while candidate in names and names[candidate] - {destination_main.absolute()}:
                    candidate = _skill_slug(group["source_root"].lstrip(".") + "-" + canonical) + "-" + str(suffix)
                    suffix += 1
                files[main], canonical = _native_skill(files[main], folder, plan.warnings, rename=candidate)
            names.setdefault(canonical, set()).add(destination_main.absolute())
        root_id, _ = _project_root(plan, project)
        for child, content in sorted(files.items()):
            relative = relative_prefix + ("/" + child if child else "")
            collision_key = (root_id, relative)
            if collision_key in occupied_paths:
                if occupied_paths[collision_key] != content:
                    raise ValueError("Conflicting project skill resource: " + relative)
                continue
            plan.files.append(PlannedFile(relative, content, "Native project skill or resource", root=root_id,
                                          mode=0o700 if child in group["executables"] else 0o600))
            occupied_paths[collision_key] = content
        tree_contents[(project_key_value, relative_prefix)] = tree_identity


class DeepSeekTarget:
    name = "deepseek"

    def plan(self, bundle, options):
        if bundle.schema_version != 1:
            raise ValueError("Unsupported migration bundle schema version")
        if options.compression not in ("zstd", "none"):
            raise ValueError("Unsupported DeepSeek compression: " + str(options.compression))
        plan = TargetPlan()
        source = bundle.source
        archive_root = "imports/" + encode_segment(source) + "/documents/"
        index = []
        global_instructions = []
        project_context = {}
        project_snapshots = {}
        for document in bundle.documents:
            if document.scope == "project":
                project_snapshots.setdefault(document.project, []).append(document)
        project_snapshot_ids = {}
        for project, documents in project_snapshots.items():
            digest = hashlib.sha256()
            for document in sorted(documents, key=lambda doc: (doc.path, doc.kind)):
                digest.update(document.path.encode("utf-8") + b"\0")
                digest.update(_document_bytes(document) + b"\0")
            project_snapshot_ids[project] = digest.hexdigest()[:20]
        skill_groups = {}
        for document in bundle.documents:
            if document.scope != "project" and document.path.startswith("skills/"):
                relative = _safe_relative(document.path)
                name = PurePosixPath(relative).parts[1]
                skill_groups.setdefault(name, {}).setdefault(document.scope, {})[relative] = _document_bytes(document)
        shadowed_skills = set()
        for name, sources in skill_groups.items():
            ordered = sorted(sources, key=lambda scope: (scope != "global", scope))
            selected = ordered[0]
            for scope in ordered[1:]:
                if sources[scope] != sources[selected]:
                    shadowed_skills.add((name, scope))
                    plan.warnings.append("同名技能 " + name + "：活动版本来自 " + selected + "，" + scope + " 版本完整保留在资料副本与迁移包中，不自动启用。")
        file_contents = {}
        overrides = {(document.scope, document.project, str(PurePosixPath(document.path).parent))
                     for document in bundle.documents
                     if PurePosixPath(document.path).name == "AGENTS.override.md" and document.content.strip()}

        def superseded(document):
            return (PurePosixPath(document.path).name == "AGENTS.md" and
                    (document.scope, document.project, str(PurePosixPath(document.path).parent)) in overrides)

        for document in bundle.documents:
            relative = _safe_relative(document.path)
            data = _document_bytes(document)
            is_project = document.scope == "project"
            if is_project:
                identity = hashlib.sha256(document.project.encode("utf-8")).hexdigest()[:16]
                destination = archive_root + "projects/" + identity + "/" + project_snapshot_ids[document.project] + "/" + relative
                project_context.setdefault(document.project, []).append((document, destination))
            elif relative.startswith("skills/"):
                skill_name = PurePosixPath(relative).parts[1]
                if (skill_name, document.scope) in shadowed_skills:
                    destination = archive_root + encode_segment(document.scope) + "/" + relative
                else:
                    destination = relative
            else:
                destination = archive_root + "global/" + relative
                if document.kind == "instruction" and not superseded(document):
                    if document.encoding == "utf-8":
                        global_instructions.append("### " + source + " " + relative + "\n\n" + document.content)
            if destination not in file_contents:
                plan.files.append(PlannedFile(destination, data, source + " " + document.kind,
                                              mode=0o700 if destination.startswith("skills/") and getattr(document, "executable", False) else 0o600))
                file_contents[destination] = data
            elif file_contents[destination] != data:
                raise ValueError("Conflicting source documents map to " + destination)
            if not relative.startswith("skills/") or destination != relative:
                where = "项目 " + _mapped_cwd(document.project, options.project_map) if is_project else "全局"
                index.append("- " + where + "：`" + str(options.home / destination) + "`")
        _project_rule_files(bundle, options, plan)
        _project_skill_files(bundle, options, plan)
        instructions = ["## 从 " + source + " 迁移的资料", "", "以下为用户原有 " + source + " 指令与资料。历史会话中的工具记录仅供参考，不要自动重放。"]
        instructions.extend(global_instructions)
        if index:
            instructions.extend(["", "### 资料索引", "", "按当前任务读取相关资料；项目资料只在对应工作目录下适用。", *index])
        # Avoid creating an empty global instruction file when only sessions move.
        if bundle.documents:
            agent_file = options.home / "AGENTS.md"
            if agent_file.is_symlink():
                raise ValueError("Refusing to plan against symlink target AGENTS.md")
            existing = agent_file.read_text(encoding="utf-8") if agent_file.exists() else ""
            plan.files.append(PlannedFile("AGENTS.md", _merge_instructions(existing, "\n\n".join(instructions), source), "Global instructions and migrated document index", merge=True))
        for session in bundle.sessions:
            mapped = _mapped_cwd(session.cwd, options.project_map) if session.cwd else ""
            cwd = mapped if mapped and os.path.isabs(mapped) else ""
            if mapped and not cwd:
                plan.warnings.append("会话 " + session.id + " 的工作目录不是本机绝对路径，已保留在上下文中；请通过 --project-map 指定目标路径。")
            context = ["这是一段从 " + source + " 导入的历史会话。请依据历史需求、结论和当前文件继续工作。",
                       "原会话 ID: " + session.id, "原标题: " + session.title,
                       "原工作目录: " + session.cwd, "目标工作目录: " + cwd,
                       "原归档状态: " + str(session.archived),
                       "工具调用、工具输出和压缩摘要已转成明确标记的历史参考资料；不会自动执行。",
                       "原始结构化记录保存在迁移 interchange bundle 中：" + str(options.home / ".migrate-agent" / "bundles")]
            for project, documents in project_context.items():
                try:
                    Path(session.cwd).expanduser().resolve().relative_to(Path(project).expanduser().resolve())
                except ValueError:
                    continue
                for document, destination in sorted(documents, key=lambda item: (len(PurePosixPath(item[0].path).parts), item[0].path)):
                    if document.kind == "instruction":
                        if superseded(document):
                            continue
                        scope = (Path(project) / PurePosixPath(document.path).parent).resolve()
                        try:
                            Path(session.cwd).expanduser().resolve().relative_to(scope)
                        except ValueError:
                            continue
                        applicable = _mapped_cwd(str(scope), options.project_map)
                        label = "项目指令 " + document.path + "（仅适用于 " + applicable + " 及其子目录）"
                        if document.encoding == "utf-8":
                            context.append("\n" + label + ":\n" + document.content)
                    elif document.path.endswith("SKILL.md") or document.kind != "skill":
                        context.append("\n历史项目资料（按需读取，不作为全局指令）: " + str(options.home / destination))
            target_id, content = native_session(session, cwd, "\n".join(context), options.compression, source)
            key = project_key(cwd) if cwd else "_no-cwd"
            suffix = ".zstd" if options.compression == "zstd" else ""
            session_directory = "sessions/" + key + "/" + encode_segment(target_id)
            opposite_suffix = "" if options.compression == "zstd" else ".zstd"
            opposite_path = options.home / session_directory / ("session.v4.jsonl" + opposite_suffix)
            if opposite_path.exists() or opposite_path.is_symlink():
                raise ValueError("目标会话 " + target_id + " 已存在另一种存储编码；请匹配原迁移与当前 Harness 的 compression 配置，或选择新的目标目录。禁止在同一会话目录混用 zstd 与 none。")
            path = session_directory + "/session.v4.jsonl" + suffix
            plan.files.append(PlannedFile(path, content, "Native DeepSeek Harness conversation"))
            plan.session_map[session.id] = target_id
        if bundle.sessions:
            plan.warnings.append(source + " 可转换的工具记录与压缩摘要作为历史参考文本保留，其余原始事件保存在迁移包；不迁移可执行工具状态、原始流式时序和客户端归档状态。")
        if bundle.settings:
            plan.warnings.append(source + " 允许迁移的配置摘要保留在迁移包中；模型、权限、MCP 与提供商设置需在 DeepSeek Harness 配置。")
        plan.instructions.append("打开或重新启动 DeepSeek Harness 以刷新外部导入的历史；已有工作区不会仅按 cwd 自动归入新会话，必要时从未分组列表移入对应工作区。目标采用 Session v4（已核对 " + TESTED_DSH_VERSION + "）。")
        plan.instructions.append("会话存储编码为 " + options.compression + "，必须与目标 session-persistence-jsonl 的 compression 配置一致；DeepSeek 默认值为 zstd。")
        if plan.project_roots:
            plan.instructions.append("项目规则已写入项目 AGENTS.md，项目技能已写入 .dsh/skills；在这些项目中新建 Harness 会话即可原生发现，无需先恢复导入历史。")
        return plan
