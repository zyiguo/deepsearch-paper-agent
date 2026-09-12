$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Call npm's real entry point to avoid a stale global npm shim.
$nodeExecutable = (Get-Command node -ErrorAction Stop).Source
$npmEntry = Join-Path (Split-Path $nodeExecutable) 'node_modules\npm\bin\npm-cli.js'
if (-not (Test-Path -LiteralPath $npmEntry)) {
    throw "没有在 Node.js 安装目录找到 npm。请修复 Node.js 安装后重试。"
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'node_modules'))) {
    & $nodeExecutable $npmEntry ci
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $nodeExecutable $npmEntry run dev
exit $LASTEXITCODE
