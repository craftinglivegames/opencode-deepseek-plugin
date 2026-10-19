# Установка зависимостей моста DeepSeek для плагина OpenCode.
# Запуск: правый клик по файлу → «Запуск с помощью PowerShell»
# (или из консоли: powershell -ExecutionPolicy Bypass -File install.ps1)
$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path   # папка плагина
$bridge = Join-Path $root "bridge"
$venv   = Join-Path $bridge "venv"
$pyExe  = Join-Path $venv "Scripts\python.exe"

Write-Host "== Плагин DeepSeek для OpenCode: установка зависимостей моста ==" -ForegroundColor Cyan

# 1. Ищем Python 3.10+
$python = $null
foreach ($cand in @("py -3", "python")) {
    try {
        $v = Invoke-Expression "$cand --version 2>&1"
        if ($v -match "Python 3\.(\d+)") {
            if ([int]$Matches[1] -ge 10) { $python = $cand; break }
        }
    } catch { }
}
if (-not $python) {
    Write-Host "Python 3.10+ не найден. Установите его с https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
Write-Host "Python: $python"

# 2. Виртуальное окружение
if (-not (Test-Path $pyExe)) {
    Write-Host "Создаю venv..."
    & $python -m venv $venv
}

# 3. Зависимости
Write-Host "Ставлю зависимости (fastapi, uvicorn, pydantic, playwright)..."
& $pyExe -m pip install --disable-pip-version-check -q -r (Join-Path $bridge "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Host "pip завершился с ошибкой" -ForegroundColor Red; exit 1 }

# 4. Браузер Playwright (если его нет — мост сам возьмёт системный Edge/Chrome)
Write-Host "Проверяю браузер Playwright (можно пропустить, если нет интернета)..."
try { & $pyExe -m playwright install chromium } catch {
    Write-Host "Браузер не скачался — мост подхватит системный Edge. Это нормально." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Готово! Теперь добавьте плагин в конфиг OpenCode (см. README.md)." -ForegroundColor Green
Write-Host "Дальше при первом запросе плагин сам поднимет мост и попросит логин/пароль от DeepSeek."
