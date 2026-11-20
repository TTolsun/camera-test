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

각 상태를 설정한 뒤 `hal begin`으로 기기 시각과 실험 정보를 기록합니다. 촬영 동작이 끝나면 `hal collect`를 바로 호출합니다. Agent는 두 명령 사이에 등록된 촬영 시나리오를 실행할 수 있습니다. 팀의 기능 설정 절차와 수동 촬영 동작도 같은 구간에 연결할 수 있습니다. `--state`는 실험의 상태를 기록합니다. 실제 설정은 팀의 개발 절차에서 적용합니다.

```powershell
# 기능을 OFF로 설정합니다.
$off = python camera-test.pyz hal begin --device $serial --state off | ConvertFrom-Json
# 같은 시작 화면에서 촬영합니다. 인물 동영상 예시입니다.
python camera-test.pyz run galaxy.portrait-video --device $serial --duration 5
$offLog = python camera-test.pyz hal collect --trial $off.trial | ConvertFrom-Json

# 기능을 ON으로 설정합니다.
$on = python camera-test.pyz hal begin --device $serial --state on | ConvertFrom-Json
python camera-test.pyz run galaxy.portrait-video --device $serial --duration 5
$onLog = python camera-test.pyz hal collect --trial $on.trial | ConvertFrom-Json
```

상태 변경 지점에서 다음 명령을 따로 실행합니다. `begin`은 `trial` 경로를, `collect`는 `logcat` 경로와 로그 레코드 수를 JSON으로 반환합니다. 로그는 `main`, `system`, `crash` 버퍼에서 읽어 기본적으로 `runs/exynos-hal/`에 저장합니다. 태그를 알고 있다면 두 `begin` 명령에 같은 `--tag` 목록을 넘길 수 있습니다. 기능 태그와 함께 세션 시작 등 촬영이 기록됐음을 보여 주는 태그도 포함합니다. [Android logcat 문서](https://developer.android.com/tools/logcat)

태그별 로그 건수 차이를 먼저 보고, 관심 태그를 지정해 메시지별로 확인합니다. 예시 태그는 해당 기기에서 관찰한 값으로 바꿉니다. `hal compare`는 저장한 `threadtime` 로그를 읽기 때문에 기기 없이도 실행할 수 있습니다.

```powershell
python camera-test.pyz hal compare --off $offLog.logcat --on $onLog.logcat
python camera-test.pyz hal compare --off $offLog.logcat --on $onLog.logcat --tag ExampleExynosTag
python camera-test.pyz hal compare --off $offLog.logcat --on $onLog.logcat `
    --tag ExampleExynosTag --expect-log '\bexample_applied=1\b' `
    --control-tag ExampleSession --control-log '^session_started$'
```

`--expect-log`는 실제 기능 적용 메시지를 찾습니다. `--control-tag`와 `--control-log`를 함께 지정하면 촬영 신호가 양쪽 로그에 있어야 `ON_ONLY`가 나옵니다. 비교 종료 코드 `0`은 요약 완료 또는 `ON_ONLY`, `2`는 그 밖의 비교 결과, `1`은 입력이나 처리 오류를 나타냅니다. `CONTROL_MISSING`은 수집 구간과 빌드의 로그 설정을 확인해야 한다는 뜻입니다.

새 OFF·ON 실험을 만들어 한 번 더 확인합니다. 기능 설정값을 출력하는 메시지보다 요청이나 프레임에 기능이 적용됐음을 보여 주는 메시지를 사용하고, PID·프레임 번호처럼 실행마다 바뀌는 값은 패턴에서 제외합니다.

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
