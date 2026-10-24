# yamem: запускатель скриптов навыка для PowerShell — то же, что `scripts/yamem` для sh.
#
#     & <навык>/scripts/yamem.ps1 start --memory <путь> --sid <sid> --part 1
#     & <навык>/scripts/yamem.ps1 --check-env
#     & <навык>/scripts/yamem.ps1 --set-python "<команда>" --memory <путь>
#
# Нужен там, где оболочка агента — PowerShell, а не bash (Codex, Kimi Work на Windows).
# Порядок выбора интерпретатора (YAMEM_PYTHON → yamem.local.yaml памяти → python3,
# python, py -3), коды возврата и текст сбоя — как у sh-версии: сбой окружения
# печатает `yamem: СБОЙ ОКРУЖЕНИЯ` и выходит с кодом 3.
# ⚠️ Файл в UTF-8 с BOM: без него Windows PowerShell 5.1 читает кириллицу как ANSI.

$ErrorActionPreference = 'Stop'
$minimumPython = '3.10'
$localConfigName = 'yamem.local.yaml'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

function Stop-EnvironmentFailure([string]$reason, [string[]]$details) {
    [Console]::Error.WriteLine("yamem: СБОЙ ОКРУЖЕНИЯ — $reason")
    foreach ($detailLine in $details) { [Console]::Error.WriteLine("  $detailLine") }
    [Console]::Error.WriteLine('  ⛔ Работу с памятью не продолжать молча: сообщить оператору и предложить варианты.')
    exit 3
}

# Пробный запуск, а не Get-Command: заглушка Microsoft Store в PATH есть, но не работает.
function Test-Python([string]$candidateCommand) {
    $commandWords = @($candidateCommand -split ' ' | Where-Object { $_ })
    $probeCode = "import sys; sys.exit(0 if sys.version_info >= tuple(int(part) for part in '$minimumPython'.split('.')) else 4)"
    $candidateArguments = @($commandWords | Select-Object -Skip 1) + @('-c', $probeCode)
    try {
        & $commandWords[0] @candidateArguments *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# Каталог памяти — где искать локальный конфиг (как в sh-версии).
$memoryArgument = $null
for ($argumentIndex = 0; $argumentIndex -lt $args.Count - 1; $argumentIndex++) {
    if ($args[$argumentIndex] -eq '--memory') { $memoryArgument = [string]$args[$argumentIndex + 1] }
}
# ⚠️ PowerShell 5.1 при `ErrorActionPreference = 'Stop'` превращает stderr внешней
# программы в исключение даже с `2>$null` — вне git-репозитория скрипт падал на
# «fatal: not a git repository» (проверено 24.10.2026 на воркстейшене; pwsh 7 так не делает).
$gitTopLevel = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
    $ErrorActionPreference = 'Continue'
    $gitTopLevel = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0) { $gitTopLevel = $null }
    $ErrorActionPreference = 'Stop'
}
$memoryCandidates = @($env:YAMEM_MEMORY, $memoryArgument)
if ($args.Count -gt 1) { $memoryCandidates += [string]$args[1] }
if ($gitTopLevel) { $memoryCandidates += @($gitTopLevel, (Join-Path $gitTopLevel '.agents/memory')) }
$memoryDir = $memoryCandidates | Where-Object {
    $_ -and ((Test-Path (Join-Path $_ 'yamem.config.yaml')) -or (Test-Path (Join-Path $_ 'MEMORY.md')))
} | Select-Object -First 1
$localConfigPath = if ($memoryDir) { Join-Path $memoryDir $localConfigName } else { $null }

$scriptName = if ($args.Count -gt 0) { [string]$args[0] } else { '' }

if ($scriptName -eq '--set-python') {
    $requestedPython = if ($args.Count -gt 1) { [string]$args[1] } else { '' }
    if (-not $requestedPython) { Stop-EnvironmentFailure 'не указана команда.' @("пример: & $scriptDir\yamem.ps1 --set-python python --memory <путь к памяти>") }
    if (-not $memoryDir) { Stop-EnvironmentFailure "не найден каталог памяти для $localConfigName." @('укажи его: --memory <путь к .agents/memory>') }
    if (-not (Test-Python $requestedPython)) { Stop-EnvironmentFailure "«$requestedPython» не запускается или старше Python $minimumPython — в конфиг не записано." @() }
    $configText = "# Машинные настройки yamem: в git не идут, на каждой машине свои.`npython: $requestedPython`n"
    [System.IO.File]::WriteAllText($localConfigPath, $configText, (New-Object System.Text.UTF8Encoding $false))
    Write-Output "yamem: записано в $localConfigPath — python: $requestedPython"
    exit 0
}

$fileConfiguredPython = $null
if ($localConfigPath -and (Test-Path $localConfigPath)) {
    $configLine = Get-Content $localConfigPath -Encoding UTF8 | Where-Object { $_ -match '^python:\s*' } | Select-Object -First 1
    if ($configLine) { $fileConfiguredPython = ($configLine -replace '^python:\s*', '').Trim().Trim('"', "'") }
}
$candidateList = if ($env:YAMEM_PYTHON) { @($env:YAMEM_PYTHON) }
                 elseif ($fileConfiguredPython) { @($fileConfiguredPython) }
                 else { @('python3', 'python', 'py -3') }
$pythonCommand = $candidateList | Where-Object { Test-Python $_ } | Select-Object -First 1
if (-not $pythonCommand) {
    Stop-EnvironmentFailure "не найден рабочий Python >= $minimumPython." @(
        "пробовал: $($candidateList -join ', ')",
        'варианты: 1) поставить Python 3 (winget install Python.Python.3.13)',
        '          2) указать интерпретатор один раз для этой машины:',
        "             & $scriptDir\yamem.ps1 --set-python ""<команда или путь>"" --memory $(if ($memoryDir) { $memoryDir } else { '<путь к памяти>' })",
        '          3) preflight вручную по SKILL.md, блок «Если стартера нет»')
}

$scriptArguments = @($args | Select-Object -Skip 1)
switch ($scriptName) {
    '--which-python' { Write-Output $pythonCommand; exit 0 }
    '--check-env' {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Stop-EnvironmentFailure 'не найден git.' @('варианты: поставить git или preflight вручную без синхронизации')
        }
        $configSource = if ($env:YAMEM_PYTHON) { 'YAMEM_PYTHON' } elseif ($fileConfiguredPython) { $localConfigPath } else { 'автоопределение' }
        Write-Output "yamem: окружение в порядке — $pythonCommand (источник: $configSource), $(git --version)"
        exit 0
    }
    '' { Stop-EnvironmentFailure 'не указан скрипт.' @("пример: & $scriptDir\yamem.ps1 start --memory <путь> --sid <sid> --part 1") }
    'start'      { $scriptName = 'yamem-start' }
    'sync'       { $scriptName = 'yamem_sync' }
    'repo-tails' { $scriptName = 'yamem-repo-tails' }
    'hosts-hook' { $scriptName = 'yamem-hosts-hook' }
}
$scriptPath = Join-Path $scriptDir (($scriptName -replace '\.py$', '') + '.py')
if (-not (Test-Path $scriptPath)) {
    $availableScripts = (Get-ChildItem $scriptDir -Filter *.py | ForEach-Object { $_.BaseName }) -join ' '
    Stop-EnvironmentFailure "нет скрипта $scriptPath." @("доступны: $availableScripts")
}
if ($scriptName -in @('yamem-start', 'yamem_sync', 'yamem-repo-tails') -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
    Stop-EnvironmentFailure 'не найден git — синхронизация памяти невозможна.' @(
        'варианты: 1) поставить git  2) preflight вручную по SKILL.md, блок «Если стартера нет»')
}

$commandWords = @($pythonCommand -split ' ' | Where-Object { $_ })
$pythonArguments = @($commandWords | Select-Object -Skip 1) + @($scriptPath) + $scriptArguments
& $commandWords[0] @pythonArguments
exit $LASTEXITCODE
