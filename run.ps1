# 启动脚本：默认 mock 模式，加 -Real 走真实模型
# API Key 从系统环境变量读取，不落文件、不进仓库
param([switch]$Real, [int]$Port = 8000)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not $env:DEEPSEEK_API_KEY) {
    $k = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'Machine')
    if (-not $k) { $k = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User') }
    if ($k) { $env:DEEPSEEK_API_KEY = $k }
}

if ($Real) {
    if (-not $env:DEEPSEEK_API_KEY) { throw '未找到 DEEPSEEK_API_KEY 环境变量' }
    $env:LLM_MOCK = '0'
    Write-Host '>>> 真实模型模式' -ForegroundColor Green
} else {
    $env:LLM_MOCK = '1'
    Write-Host '>>> Mock 模式（不消耗 token）' -ForegroundColor Yellow
}

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw '未找到 .venv，先执行：python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt' }

Write-Host ">>> http://127.0.0.1:$Port" -ForegroundColor Cyan
& $py -m uvicorn app.main:app --reload --host 127.0.0.1 --port $Port
