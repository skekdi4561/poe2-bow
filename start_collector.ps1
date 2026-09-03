# PoE2 시세 감정소 수집기 실행 스크립트 — 더블클릭용(start_collector.cmd 가 이 파일을 부른다).
# POESESSID 를 매번 치지 않도록, 첫 실행 때 한 번 입력받아 Windows 사용자 계정 키(DPAPI)로 암호화해
# %APPDATA%\poe2-sise-collector\poesessid.dpapi 에 저장한다. 저장소 폴더 밖이라 커밋될 일이 없고,
# 이 Windows 계정으로 로그인한 사람만 풀 수 있다. 값을 바꾸려면: start_collector.cmd -Reset
param([switch]$Reset)
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
Write-Host "수집기 시작 — 이 창을 닫으면 수집이 멈춥니다. 끄려면 Ctrl+C"
python serve.py --collect --every 3600 --weapons --push
