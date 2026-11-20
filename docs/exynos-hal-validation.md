# Exynos Camera HAL 검사 만들기

Exynos 실기기에서 기능을 끈 상태와 켠 상태의 로그를 비교해, 기능이 실제로 적용됐음을 확인할 수 있는 검사 정의를 만듭니다. 아래 명령은 Windows PowerShell과 ADB를 기준으로 합니다.

## 1. 기기와 비교 조건을 확인합니다

```powershell
adb devices -l
$serial = 'REPLACE_WITH_ADB_SERIAL'
python camera-test.pyz inspect --device $serial --pretty
```

`device.soc_vendor`가 `exynos`인지 확인합니다. 비교에 사용할 펌웨어·앱 버전, 모드, 렌즈, 해상도, 프레임레이트, 촬영 조건을 기록합니다. 기능 설정은 팀이 사용하는 개발 절차로 바꾸고, 설정 변경에 앱 재시작이나 새 카메라 세션이 필요하면 두 조건에 같은 절차를 적용합니다. `scenario.status`가 `runnable`이면 등록된 인물 동영상 시나리오를 실행할 수 있습니다. 다른 모드나 앱 버전은 아래 방식으로 수동 재현해 로그를 확인할 수 있습니다.

## 2. OFF와 ON에서 같은 동작을 재현합니다

아래 함수를 저장소 루트에서 한 번 정의합니다. 매번 기능 상태를 설정한 뒤 `Save-HalTrial`을 호출하고, 기기에서 같은 촬영 동작을 끝낸 다음 Enter를 누릅니다. 테스트 구간의 `main`, `system`, `crash` 버퍼가 로컬 파일에 저장됩니다. [Android logcat 문서](https://developer.android.com/tools/logcat)

```powershell
$trialDir = Join-Path 'runs/exynos-hal' (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
New-Item -ItemType Directory -Path $trialDir -Force | Out-Null

function Save-HalTrial {
    param([Parameter(Mandatory)][ValidatePattern('^[a-z0-9-]+$')][string]$Trial)

    $logPath = Join-Path $trialDir "$Trial.log"
    if (Test-Path -LiteralPath $logPath) { throw "Trial already captured: $Trial" }

    $since = & adb -s $serial shell 'date "+%m-%d %H:%M:%S.%3N"'
    if ($LASTEXITCODE -ne 0 -or "$since" -notmatch '^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$') {
        throw 'Could not read the device clock.'
    }

    Read-Host 'Reproduce the camera action on the device, then press Enter' | Out-Null
    $lines = & adb -s $serial shell "logcat -d -T '$since' -v threadtime -b main -b system -b crash"
    if ($LASTEXITCODE -ne 0 -or -not $lines) { throw 'Could not capture logcat.' }
    $lines | Set-Content -LiteralPath $logPath -Encoding utf8
    Get-Item -LiteralPath $logPath
}

Read-Host 'Set the feature to OFF and prepare the start screen, then press Enter' | Out-Null
Save-HalTrial -Trial 'off-01'
Read-Host 'Set the feature to ON and prepare the same start screen, then press Enter' | Out-Null
Save-HalTrial -Trial 'on-01'
```

두 파일에서 촬영 요청이나 세션 시작처럼 **양쪽 실행이 모두 기록됐음을 보여 주는 공통 신호**를 먼저 확인합니다. 이어 실제 기능 적용을 나타내는 태그와 메시지를 찾습니다. 후보 태그가 있으면 `Select-String`으로 좁혀 비교합니다.

```powershell
$tag = 'REPLACE_WITH_OBSERVED_TAG'
$tagPattern = '^\S+\s+\S+\s+\d+\s+\d+\s+[VDIWEFAS]\s+' + [regex]::Escape($tag) + '\s*:'
Select-String -LiteralPath (Join-Path $trialDir 'off-01.log') -Pattern $tagPattern -CaseSensitive
Select-String -LiteralPath (Join-Path $trialDir 'on-01.log') -Pattern $tagPattern -CaseSensitive
```

`off-02`, `on-02`처럼 새 이름으로 한 번 더 비교합니다. 기능 설정값을 출력하는 메시지보다 요청이나 프레임에 기능이 적용됐음을 보여 주는 메시지를 사용하고, PID·프레임 번호처럼 실행마다 바뀌는 값은 패턴에서 제외합니다. 공통 신호가 없으면 수집 구간과 빌드의 로그 설정을 먼저 확인합니다. OFF에서 로그 한 줄이 보이지 않는다는 사실만으로는 기능 상태를 판정하기 어렵습니다.

## 3. 검사 정의를 등록하고 재실행합니다

팀 내부 검사 파일에 아래 형식으로 등록합니다. 예시 이름·태그·정규식은 실제로 관찰한 값으로 바꿉니다. `expect_log`는 지정한 태그의 **메시지 본문**에 적용하는 Python 정규식입니다. JSON에 정규식의 역슬래시를 적을 때는 `\\`로 입력합니다.

```json
{
  "schema_version": 1,
  "checks": [
    {
      "name": "my-feature",
      "source": "hal",
      "vendor": "exynos",
      "tag": "ExampleExynosTag",
      "expect_log": "\\bexample_applied=1\\b"
    }
  ]
}
```

인물 동영상에서 재현할 수 있는 기능은 같은 명령을 OFF와 ON에서 각각 실행합니다. 로컬에서 작업한다면 검사 파일을 Git이 무시하는 `runs/` 아래에 둘 수 있습니다.

```powershell
$checks = 'runs/exynos-hal/checks.local.json'
python camera-test.pyz inspect --device $serial --checks-file $checks --pretty
python camera-test.pyz run galaxy.portrait-video --device $serial --check my-feature --checks-file $checks
```

- `inspect` 결과의 `checks.registered_for_device`에 검사 이름이 표시되는지 확인합니다.
- ON에서 `scenario_status`와 `feature.status`가 모두 `PASS`인지 확인합니다.
- OFF에서 `scenario_status`가 `PASS`이고 `feature.reason`이 `EXPECTED_LOG_NOT_FOUND`인지 확인합니다. 이때 CLI 종료 코드 `2`는 예상한 결과입니다. 검사 미등록, SoC 불일치, 로그 수집 오류는 별도로 해결합니다.
- 현재 자동 검사는 녹화 버튼을 누르기 직전부터 수집한 로그에서 태그 하나와 메시지 패턴 하나를 찾습니다. 기능 신호가 앱을 열 때만 나오거나 다른 촬영 동작·로그 소스·여러 상태 전이가 필요하면, 수동으로 확보한 근거를 바탕으로 수집 구간이나 시나리오를 확장한 뒤 자동 판정을 검증합니다.

최종 기록에는 재현 동작, ON/OFF 결과, 검증한 빌드, 적용 신호를 남깁니다. 사내 HAL 태그, 원본 로그, 실제 검사 정의는 사내 승인 저장소에서 관리합니다.
