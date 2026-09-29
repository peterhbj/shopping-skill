$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$project = $PSScriptRoot
$venvPython = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonCommand = Get-Command python, py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonCommand) {
        $basePython = $pythonCommand.Source
    } else {
        $basePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    }
    if (-not (Test-Path -LiteralPath $basePython)) {
        throw 'Python não encontrado. Instale Python 3.12+ antes de iniciar.'
    }
    & $basePython -m venv (Join-Path $project '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar .venv' }
}
& $venvPython -c 'import playwright, yaml' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $venvPython -m pip install -r (Join-Path $project 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependências Python' }
}
Set-Location -LiteralPath $project
& $venvPython -m orchestrator.webapp
