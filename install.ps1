<#
  安装 Zotero Paper Fetcher skill 到 Claude Code 和 Codex。

    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1          复制
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Link    目录联接（改仓库即生效，适合开发）

  同时在 ~/.config/academic-agent/ 下生成配置模板（已存在则不动）。
#>
param([switch]$Link)
$ErrorActionPreference = 'Stop'

$src = Join-Path $PSScriptRoot 'skills\zotero-paper-fetcher'
$targets = @(
  (Join-Path $env:USERPROFILE '.claude\skills'),
  (Join-Path $env:USERPROFILE '.codex\skills')
)

foreach ($root in $targets) {
  $agent = Split-Path (Split-Path $root -Parent) -Leaf
  if (-not (Test-Path (Split-Path $root -Parent))) { "跳过 $agent（未安装）"; continue }
  New-Item -ItemType Directory -Force $root | Out-Null
  $dst = Join-Path $root 'zotero-paper-fetcher'
  if (Test-Path $dst) {
    $item = Get-Item $dst -Force
    if ($item.LinkType) { $item.Delete() } else { Remove-Item $dst -Recurse -Force -Confirm:$false }
  }
  if ($Link) {
    New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
    "联接 $dst -> $src"
  } else {
    Copy-Item $src $dst -Recurse
    "复制到 $dst"
  }
}

$cfgDir = Join-Path $env:USERPROFILE '.config\academic-agent'
$cfg = Join-Path $cfgDir 'paperfetch.json'
if (-not (Test-Path $cfg)) {
  New-Item -ItemType Directory -Force $cfgDir | Out-Null
  Copy-Item (Join-Path $PSScriptRoot 'config.example.json') $cfg
  "已生成配置模板 $cfg（填入 Elsevier API key、Wiley TDM token，可留空）"
}

foreach ($t in 'uv', 'pdfinfo', 'pdftotext') {
  if (-not (Get-Command $t -ErrorAction SilentlyContinue)) {
    $tl = Get-ChildItem 'C:\texlive\*\bin\win*' -Filter "$t.exe" -ErrorAction SilentlyContinue
    if (-not $tl) { "缺少 $t：uv 见 https://docs.astral.sh/uv/ ，pdfinfo/pdftotext 装 Poppler 或 TeX Live" }
  }
}
