#!/usr/bin/env bash
# ascend-tune-lab — configuration-tuning-agents installer (CANNBot-style)

set -euo pipefail

if [ -t 1 ]; then
  GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'
  CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
else
  GREEN=''; YELLOW=''; RED=''; CYAN=''; BOLD=''; DIM=''; NC=''
fi

ok()   { echo -e "  ${DIM}${GREEN}✓${NC}${DIM} $*${NC}"; }
warn() { echo -e "  ${YELLOW}⚠${NC}${DIM} $*${NC}"; }
err()  { echo -e "  ${RED}✗${NC}${DIM} $*${NC}"; }
info() { echo -e "  ${DIM}${CYAN}→${NC}${DIM} $*${NC}"; }
step() { echo -e "${DIM}$*${NC}"; }

detect_trae_variant() {
  if [ -d "$HOME/.trae-cn" ]; then TRAE_VARIANT="ide"
  elif [ -d "$HOME/.marscode" ]; then TRAE_VARIANT="plugin"
  elif [ -d "$HOME/.traecli" ]; then TRAE_VARIANT="cli"
  else TRAE_VARIANT="unknown"
  fi
}

BRAND="ascend-tune-lab"
TEAM="configuration-tuning-agents"
VERSION="0.1.0"

INCLUDED_SKILLS="ascend-baseline-generator serving-cfg-extract serving-perf-metrics vllm-ascend-config-extractor model-feature-extractor serving-parallel-strategy-tuning find-possible-parallel-strategy serving-kv-cache-capacity serving-slo-concurrency"
INCLUDED_AGENT_PATTERN="serving-*"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$SCRIPT_DIR"
REPO_ROOT="$(cd "$PLUGIN_ROOT/.." && pwd)"
LOCAL_AGENT_ROOT="$PLUGIN_ROOT/agents"
SKILL_ROOT="$REPO_ROOT/configuration-tuning-skills"

show_help() {
  cat <<EOF
ascend-tune-lab — vLLM-Ascend 服务化性能优化 Agent 安装脚本

Usage: init.sh [level] [tool] [install_path]

Arguments:
  level        project (default) | global
  tool         opencode (default) | claude | trae | cursor | copilot | codearts
  install_path 项目安装目录（默认：当前工作目录）

安装内容:
  - AGENTS.md（primary: serving-perf-optimization）
  - agents/*.md（subagent 符号链接）
  - skills/*（configuration-tuning-skills 符号链接）
  - workflows/（工作流、模板、派发脚本 — 符号链接）
  - configuration-tuning-skills/、configuration-tuning-agents/（仓库路径符号链接，便于 skill 内绝对路径）

Examples:
  ./init.sh project cursor
  ./init.sh project cursor /path/to/your/serving/project
  ./init.sh project opencode
EOF
}

LEVEL="project"
TOOL="opencode"
INSTALL_PATH=""

for arg in "$@"; do
  case "$arg" in
    --help) show_help; exit 0 ;;
    global|project) LEVEL="$arg" ;;
    opencode|claude|trae|cursor|copilot|codearts) TOOL="$arg" ;;
  esac
done

if [ "$#" -gt 0 ]; then
  last_arg="${!#}"
  case "$last_arg" in
    --help|global|project|opencode|claude|trae|cursor|copilot|codearts) ;;
    *) INSTALL_PATH="$last_arg" ;;
  esac
fi

if [ "$LEVEL" = "global" ]; then
  case "$TOOL" in
    opencode) CONFIG_ROOT="$HOME/.config/opencode" ;;
    trae)
      detect_trae_variant
      case "$TRAE_VARIANT" in
        plugin) CONFIG_ROOT="$HOME/.marscode" ;;
        cli)    CONFIG_ROOT="$HOME/.traecli" ;;
        *)      CONFIG_ROOT="$HOME/.trae-cn" ;;
      esac
      ;;
    cursor)   CONFIG_ROOT="$HOME/.cursor" ;;
    copilot)  CONFIG_ROOT="$HOME/.copilot" ;;
    codearts) CONFIG_ROOT="$HOME/.codeartsdoer" ;;
    *)        CONFIG_ROOT="$HOME/.claude" ;;
  esac
  CONFIG_ROOT_BASE="$CONFIG_ROOT"
else
  if [ -n "$INSTALL_PATH" ]; then
    CONFIG_ROOT_BASE="$(cd "$INSTALL_PATH" && pwd)"
  else
    CONFIG_ROOT_BASE="$PWD"
  fi
  case "$TOOL" in
    opencode) CONFIG_ROOT="$CONFIG_ROOT_BASE/.opencode" ;;
    trae)
      detect_trae_variant
      case "$TRAE_VARIANT" in
        plugin) CONFIG_ROOT="$CONFIG_ROOT_BASE/.marscode" ;;
        cli)    CONFIG_ROOT="$CONFIG_ROOT_BASE/.traecli" ;;
        *)      CONFIG_ROOT="$CONFIG_ROOT_BASE/.trae" ;;
      esac
      ;;
    cursor)   CONFIG_ROOT="$CONFIG_ROOT_BASE/.cursor" ;;
    copilot)  CONFIG_ROOT="$CONFIG_ROOT_BASE/.github" ;;
    codearts) CONFIG_ROOT="$CONFIG_ROOT_BASE/.codeartsdoer" ;;
    *)        CONFIG_ROOT="$CONFIG_ROOT_BASE/.claude" ;;
  esac
fi

TUNE_DIR="$CONFIG_ROOT/$BRAND"

realpath_safe() {
  if command -v realpath >/dev/null 2>&1; then
    realpath "$1"
  else
    python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
  fi
}

install_skill_links() {
  local target_root="$1"
  mkdir -p "$target_root"
  local count=0
  for skill in $INCLUDED_SKILLS; do
    local src="$SKILL_ROOT/$skill"
    if [ -d "$src" ]; then
      rm -rf "$target_root/$skill"
      ln -sfn "$(realpath_safe "$src")" "$target_root/$skill"
      count=$((count + 1))
    else
      warn "Skill not found: $src"
    fi
  done
  ok "Skills: $count linked → $target_root"
}

install_agent_links() {
  local target_root="$1"
  mkdir -p "$target_root"
  local count=0
  for agent_entry in "$LOCAL_AGENT_ROOT"/*.md; do
    [ -f "$agent_entry" ] || continue
    local name base
    name=$(basename "$agent_entry")
    base="${name%.md}"
    case "$base" in
      $INCLUDED_AGENT_PATTERN) ;;
      *) continue ;;
    esac
    rm -f "$target_root/$name"
    ln -sfn "$(realpath_safe "$agent_entry")" "$target_root/$name"
    count=$((count + 1))
  done
  ok "Agents: $count linked → $target_root"
}

install_config() {
  local config_src="$PLUGIN_ROOT/AGENTS.md"
  local config_name="AGENTS.md"
  if [ "$TOOL" = "claude" ] && [ "$LEVEL" = "project" ]; then
    config_name="CLAUDE.md"
  elif [ "$TOOL" = "claude" ] && [ "$LEVEL" = "global" ]; then
    config_name="CLAUDE.md"
  fi

  local config_target
  if [ "$LEVEL" = "project" ]; then
    config_target="$CONFIG_ROOT_BASE/$config_name"
  else
    config_target="$CONFIG_ROOT/$config_name"
  fi

  mkdir -p "$CONFIG_ROOT"
  if [ -e "$config_target" ] && [ ! -L "$config_target" ] && [ "$PLUGIN_ROOT/AGENTS.md" != "$config_target" ]; then
    warn "$(basename "$config_target") exists; replacing with symlink to plugin AGENTS.md"
  fi
  ln -sfn "$(realpath_safe "$config_src")" "$config_target"
  ok "$(basename "$config_target") → $config_target"

  if [ "$LEVEL" = "project" ] && [ "$config_target" != "$CONFIG_ROOT/$config_name" ]; then
    ln -sfn "$(realpath_safe "$config_src")" "$CONFIG_ROOT/$config_name"
    ok "$(basename "$config_name") → $CONFIG_ROOT/$config_name"
  fi
}

install_workflows() {
  local wf_src="$PLUGIN_ROOT/workflows"
  [ -d "$wf_src" ] || { warn "workflows/ not found in plugin"; return 0; }

  mkdir -p "$CONFIG_ROOT"
  rm -f "$CONFIG_ROOT/workflows"
  ln -sfn "$(realpath_safe "$wf_src")" "$CONFIG_ROOT/workflows"
  ok "workflows → $CONFIG_ROOT/workflows"

  if [ "$LEVEL" = "project" ]; then
    rm -f "$CONFIG_ROOT_BASE/workflows"
    ln -sfn "$(realpath_safe "$wf_src")" "$CONFIG_ROOT_BASE/workflows"
    ok "workflows → $CONFIG_ROOT_BASE/workflows (project root)"
  fi
}

install_repo_path_links() {
  if [ "$LEVEL" != "project" ]; then
    return 0
  fi
  for spec in "$SKILL_ROOT:configuration-tuning-skills" "$PLUGIN_ROOT:configuration-tuning-agents"; do
    local src="${spec%%:*}"
    local repo_name="${spec#*:}"
    local dest="$CONFIG_ROOT_BASE/$repo_name"
    local src_abs dest_abs
    src_abs="$(realpath_safe "$src")"
    if [ -e "$dest" ]; then
      dest_abs="$(realpath_safe "$dest")"
      if [ "$src_abs" = "$dest_abs" ]; then
        info "$repo_name already present in-repo, skip symlink"
        continue
      fi
    fi
    if [ -d "$dest" ] && [ ! -L "$dest" ]; then
      warn "$repo_name exists as directory; skip symlink (use in-repo paths)"
      continue
    fi
    rm -f "$dest"
    ln -sfn "$src_abs" "$dest"
    ok "$repo_name → $dest"
  done
}

write_manifest() {
  local manifest="$CONFIG_ROOT/${BRAND}-manifest.json"
  local skills_json agents_json
  skills_json=$(printf '%s\n' $INCLUDED_SKILLS | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")
  agents_json=$(find "$LOCAL_AGENT_ROOT" -maxdepth 1 -name 'serving-*.md' -printf '%f\n' 2>/dev/null | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))" || echo '[]')
  cat > "$manifest" <<EOF
{
  "brand": "$BRAND",
  "version": "$VERSION",
  "team": "$TEAM",
  "level": "$LEVEL",
  "tool": "$TOOL",
  "workflow_entry": "workflows/serving-perf-optimization-workflow.md",
  "installed_skills": $skills_json,
  "installed_agents": $agents_json,
  "config_root": "$CONFIG_ROOT",
  "install_time": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
  ok "Manifest: $manifest"
}

show_banner() {
  echo ""
  echo -e "${CYAN}${BOLD}ascend-tune-lab${NC}${DIM} · configuration-tuning-agents${NC}"
  echo ""
}

show_banner
echo "  Tool:   $TOOL"
echo "  Level:  $LEVEL"
echo "  Config: $CONFIG_ROOT"
echo "  Project: $CONFIG_ROOT_BASE"
echo ""

step "[1/5] Skills & agents..."
mkdir -p "$TUNE_DIR"
install_skill_links "$TUNE_DIR/skills"
install_agent_links "$CONFIG_ROOT/agents"
echo ""

step "[2/5] Primary config..."
install_config
echo ""

step "[3/5] Workflows..."
install_workflows
echo ""

step "[4/5] Repo path links..."
install_repo_path_links
echo ""

step "[5/5] Manifest & health check..."
write_manifest

health_ok=true
[ -L "$CONFIG_ROOT/workflows" ] || [ -d "$CONFIG_ROOT/workflows" ] || { err "workflows mount missing under $CONFIG_ROOT"; health_ok=false; }
[ -f "$CONFIG_ROOT/workflows/serving-perf-optimization-workflow.md" ] || { err "workflow entry missing"; health_ok=false; }
[ -d "$TUNE_DIR/skills" ] || { err "skills dir missing"; health_ok=false; }
[ -d "$CONFIG_ROOT/agents" ] || { err "agents dir missing"; health_ok=false; }

if [ "$health_ok" = true ]; then
  ok "Health check passed"
else
  exit 1
fi

echo ""
echo -e "  ${GREEN}${BOLD}✓ configuration-tuning-agents installed${NC}"
echo ""
echo -e "  ${BOLD}Quick start:${NC}"
echo -e "  ${CYAN}1.${NC} 在目标项目打开 Agent（$TOOL）"
echo -e "  ${CYAN}2.${NC} 启动 Agent；未指定工作目录时使用 ${DIM}./workspace${NC}；若无 ${DIM}deploy-config.md${NC}，将自动生成模板，填完 ${DIM}## 基本参数${NC} 后重新发起"
echo -e "  ${CYAN}3.${NC} Primary 将 Read：${DIM}workflows/serving-perf-optimization-workflow.md${NC}"
echo ""
