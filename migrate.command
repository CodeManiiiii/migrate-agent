#!/bin/zsh
# Double-click on macOS, or pass the same options as `migrate-agent migrate`.
set -u
cd -- "$(dirname -- "$0")" || exit 1
task_python="${MIGRATE_AGENT_PYTHON:-python3}"
"$task_python" -m migrate_agent migrate "$@"
task_status=$?
if [[ -t 0 ]]; then
  read -r "?按回车关闭窗口…"
fi
exit "$task_status"
