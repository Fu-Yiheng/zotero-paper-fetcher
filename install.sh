#!/usr/bin/env bash
# 安装 fetch-paper skill 到 Claude Code 和 Codex。
#   ./install.sh          复制
#   ./install.sh --link   软链接（改仓库即生效）
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/skills/fetch-paper"

for root in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
  agent_dir="$(dirname "$root")"
  if [ ! -d "$agent_dir" ]; then echo "跳过 $agent_dir（未安装）"; continue; fi
  mkdir -p "$root"
  dst="$root/fetch-paper"
  rm -rf "$dst"
  if [ "${1:-}" = "--link" ]; then ln -s "$src" "$dst"; echo "链接 $dst -> $src"
  else cp -R "$src" "$dst"; echo "复制到 $dst"; fi
done

cfg="$HOME/.config/academic-agent/paperfetch.json"
if [ ! -f "$cfg" ]; then
  mkdir -p "$(dirname "$cfg")"
  cp "$here/config.example.json" "$cfg"
  chmod 600 "$cfg"
  echo "已生成配置模板 $cfg（填入 Elsevier API key、Wiley TDM token，可留空）"
fi

for t in uv pdfinfo pdftotext; do
  command -v "$t" >/dev/null || echo "缺少 $t：uv 见 https://docs.astral.sh/uv/ ，pdfinfo/pdftotext 装 poppler"
done
