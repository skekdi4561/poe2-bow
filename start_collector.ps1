# PoE2 시세 감정소 수집기 실행 스크립트 — 더블클릭용(start_collector.cmd 가 이 파일을 부른다).
# POESESSID 를 매번 치지 않도록, 첫 실행 때 한 번 입력받아 Windows 사용자 계정 키(DPAPI)로 암호화해
# %APPDATA%\poe2-sise-collector\poesessid.dpapi 에 저장한다. 저장소 폴더 밖이라 커밋될 일이 없고,
# 이 Windows 계정으로 로그인한 사람만 풀 수 있다. 값을 바꾸려면: start_collector.cmd -Reset
#
# 새 리그가 열린 날(지난 리그 수집분을 비우고 새로 시작):
#   start_collector.cmd -NewLeague -Url "https://poe.kakaogames.com/trade2/search/poe2/<새리그>/<검색ID>"
#   두 번째 실행부터는 그냥 더블클릭하면 된다 — URL 은 snapshots.db 에 기억된다.
param([switch]$Reset, [switch]$NewLeague, [string]$Url)
$ErrorActionPreference = "Stop"
$store = Join-Path $env:APPDATA "poe2-sise-collector"
$file  = Join-Path $store "poesessid.dpapi"

if ($Reset -and (Test-Path $file)) { Remove-Item $file; Write-Host "저장된 값을 지웠습니다. 다시 입력합니다." }
if (-not (Test-Path $file)) {
  New-Item -ItemType Directory -Force $store | Out-Null
  Write-Host "거래소 세션 쿠키(POESESSID) 값을 붙여넣고 Enter (입력은 화면에 안 보입니다)"
  Write-Host "  값 찾기: 거래소 로그인 상태에서 F12 → 애플리케이션 → 쿠키 → poe.kakaogames.com → POESESSID"
  $sec = Read-Host -AsSecureString
  $sec | ConvertFrom-SecureString | Set-Content $file -Encoding ASCII
  Write-Host "저장했습니다: $file"
}

$sec  = Get-Content $file | ConvertTo-SecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try   { $env:POESESSID = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }

if ($env:POESESSID.Length -ne 32) {
  Write-Warning "저장된 값의 길이가 32가 아닙니다($($env:POESESSID.Length)). 잘못 붙여넣었으면 start_collector.cmd -Reset 으로 다시 입력하세요."
}

Set-Location $PSScriptRoot

# 새 리그 초기화 — 지난 리그 수집분을 지우지 않고 백업 폴더로 **옮긴다**(언제든 되돌릴 수 있게).
# snapshots.db 한 파일에 스냅샷·24시간 합집합·마지막 검색 URL 이 전부 들어 있어서 이것만 치우면
# 새 리그가 빈 상태에서 시작한다. 공개 파일(latest*.json)은 첫 수집이 덮어쓰므로 건드리지 않는다.
if ($NewLeague) {
  if (-not $Url) {
    Write-Error "새 리그 검색 URL 이 필요합니다. 예: start_collector.cmd -NewLeague -Url `"https://poe.kakaogames.com/trade2/search/poe2/<새리그>/<검색ID>`""
    exit 1
  }
  $lock = Join-Path $PSScriptRoot "collector.lock"
  if (Test-Path $lock) {
    $pidText = (Get-Content $lock -Raw).Trim()
    $running = $null
    if ($pidText -match '^\d+$') {
      try { $running = Get-Process -Id ([int]$pidText) -ErrorAction Stop } catch { $running = $null }
    }
    if ($running) {
      Write-Error "수집기가 아직 돌고 있습니다 (PID $pidText). 그 창에서 Ctrl+C 로 멈춘 뒤 다시 실행하세요."
      exit 1
    }
  }
  $db = Join-Path $PSScriptRoot "snapshots.db"
  if (Test-Path $db) {
    $backup = Join-Path $store ("backup_" + (Get-Date -Format "yyyyMMdd_HHmm"))
    New-Item -ItemType Directory -Force $backup | Out-Null
    Move-Item $db (Join-Path $backup "snapshots.db")
    Write-Host "지난 리그 수집분을 옮겼습니다: $backup\snapshots.db"
    Write-Host "  (되돌리려면 이 파일을 저장소 폴더로 다시 옮기면 됩니다)"
  } else {
    Write-Host "옮길 수집분이 없습니다(snapshots.db 없음) — 그대로 새로 시작합니다."
  }
  Write-Host "새 리그로 수집을 시작합니다. 공개 곡선은 첫 사이클이 끝나면 새 리그 값으로 바뀝니다."
}

Write-Host "수집기 시작 — 이 창을 닫으면 수집이 멈춥니다. 끄려면 Ctrl+C"
if ($Url) {
  python serve.py --collect $Url --every 3600 --weapons --push
} else {
  python serve.py --collect --every 3600 --weapons --push
}
