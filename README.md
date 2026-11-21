# 카메라 실기기 검증 도구

개발자와 사내 AI Agent가 같은 명령으로 ADB에 연결된 갤럭시의 카메라 기능을 확인합니다. `inspect`는 기기와 모드를 확인합니다. `run`은 등록된 시나리오를 재현하고 로그와 결과를 저장합니다.

## GitHub에서 내려받아 실행

저장소를 클론하거나 ZIP으로 내려받은 뒤 저장소 루트에서 실행합니다. Python 3.10 이상과 [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)가 필요합니다. 테스트 단말을 ADB로 연결하고 잠금을 해제합니다.

```powershell
python camera-test.pyz inspect --pretty
python camera-test.pyz run galaxy.portrait-video --duration 5
```

실기기가 한 대이면 자동으로 선택합니다. 여러 대이면 `adb devices -l`에 표시되는 식별자를 `--device <serial>`로 지정합니다. ADB가 PATH 또는 기본 SDK 경로에 없으면 `CAMERA_TEST_ADB`에 실행 파일 경로를 설정합니다.

## 현재 기기의 기능 확인

- **기기와 카메라 서비스**에서 모델, Android 버전, SoC, 갤럭시 카메라 앱 버전, 시스템이 보고한 카메라 공급자를 확인합니다.
- **관찰된 모드**에는 현재 모드 목록과 더보기에서 UI가 노출한 이름이 표시됩니다. 탐색이 끝나면 기존 카메라 모드로 복귀를 시도합니다. `inspect --no-ui`는 카메라를 열지 않고 기기와 서비스 정보만 읽습니다.
- **시나리오 상태**가 `runnable`이면 대상 앱 버전이 맞고 인물 동영상 모드가 관찰된 상태입니다. 실제 녹화와 저장 결과는 `run`에서 판정합니다. UI에서 발견한 다른 모드도 목록에 표시됩니다. 현재 실행 시나리오는 `galaxy.portrait-video` 한 개입니다.
- **등록된 검사**는 `inspect --checks-file <path>`로 확인합니다. HAL 검사는 감지된 SoC에 해당하는 등록 항목만 표시합니다.

## 실행 결과와 지원 범위

시나리오는 인물 동영상 모드로 이동하고, 카메라 앱의 녹화 상태 로그를 확인한 뒤 요청한 시간만큼 녹화합니다. 종료 로그와 `DCIM/Camera`에 생성된 새 MP4의 저장을 확인합니다. 실행 결과는 JSON 한 줄로 반환되며, `runs/<run-id>/`에 `result.json`과 선택한 태그의 `logcat.txt`를 저장합니다. 카메라 화면에서 실패하면 캡처를 시도합니다. `--pull-video`를 추가하면 새 영상도 PC로 복사합니다.

내장 앱 프로필은 `SM-S936N`, Android 17, Qualcomm `SM8750`, 갤럭시 카메라 `17.0.00.43`의 한국어 환경에서 검증했습니다. 프로필의 `verified_versions`에 없는 앱 버전에서는 조작 전에 중단합니다. Exynos 실기기 검증에는 팀에서 사용하는 앱 빌드와 Camera HAL 로그 정의가 필요합니다.

## 새 카메라 앱 빌드 대응

시나리오가 의존하는 리소스 ID, UI 문구, 녹화 상태 로그는 코드가 아니라 앱 프로필(JSON)에 있습니다. 앱 빌드가 바뀌어도 대부분은 코드를 수정하거나 `camera-test.pyz`를 다시 빌드하지 않고 프로필 파일만 고쳐서 대응할 수 있습니다.

1. 새 버전에서 그대로 동작하는지 먼저 확인합니다. 판정은 녹화 상태 로그와 새 MP4 저장에 근거하므로, 선택자가 틀리면 PASS가 아니라 오류로 끝납니다.

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --allow-unverified-version
   ```

2. 통과하면 내장 프로필을 파일로 내보내고, `verified_versions`에 새 버전을 추가합니다. PowerShell의 `>` 리디렉션 대신 `--output`을 사용합니다.

   ```powershell
   python camera-test.pyz profile show galaxy.portrait-video --output galaxy-camera.json
   ```

3. 실패하면 결과 JSON의 `diagnosis`를 확인합니다.
   - `diagnosis.phase`: 실패한 단계(`launch`, `mode_select`, `mode_ready`, `recording`)입니다.
   - `diagnosis.ui.absent`: 마지막 화면에서 **실패한 단계가 의존하는** 선택자 중 찾지 못한 것과, 이름이 비슷한 리소스 ID 후보(`similar`)입니다. 그 단계에서 필요 없는 선택자(예: 인물 동영상 화면의 `mode_strip`)는 목록에 나오지 않습니다. 화면 전체는 `artifacts.failure_ui`에 저장됩니다.
   - `diagnosis.record_state_log`: `lines`가 0보다 크고 `matched`가 0이면 녹화 상태 로그의 문구가 바뀐 것이므로 `log.record_state.pattern`을 고칩니다.
4. 프로필을 고친 뒤 ADB 없이 형식을 검사하고, 실기기에서 다시 실행합니다. 알 수 없는 키나 오타가 있는 선택자 이름은 검사 단계에서 거부됩니다.

   ```powershell
   python camera-test.pyz profile validate galaxy-camera.json
   python camera-test.pyz run galaxy.portrait-video --profile-file galaxy-camera.json
   ```

5. 팀 전체에 반영하려면 수정한 값을 `camera_runner/scenarios/galaxy_portrait_video.py`의 `PROFILE`에 옮기고 `camera-test.pyz`를 다시 빌드합니다.

`inspect`의 `scenario.status`가 `unverified`이면 현재 프로필의 선택자로 모드는 찾았지만 앱 버전이 검증 목록에 없다는 뜻입니다. `--allow-unverified-version`으로 실행한 결과에는 `warnings: ["UNVERIFIED_CAMERA_VERSION"]`이 붙습니다.

## 실행 전 환경 점검(preflight)

`run`과 UI를 탐색하는 `inspect`는 화면을 조작하기 전에 환경을 점검하고, 측정값을 `preflight`에 기록합니다.

| 오류 코드 | 조건 |
|---|---|
| `PRECHECK_SCREEN_OFF` | `dumpsys power`의 `mWakefulness`가 `Awake`가 아닙니다(`Asleep`, `Dozing`). |
| `PRECHECK_LOCKED` | `dumpsys window`의 `isKeyguardShowing=true`(구형 Android는 `mShowingLockscreen=true`)입니다. |
| `PRECHECK_LOW_STORAGE` | `df -k /sdcard`의 사용 가능 공간이 `camera_runner/preflight.py`의 `MIN_FREE_BYTES`(500 MB) 미만입니다. |

Android 버전에 따라 해당 줄이 없으면 그 항목은 `unknown`으로 기록하고 실패로 처리하지 않습니다. `inspect`는 점검에 실패하면 화면을 탐색하지 않고 `preflight.failed`와 `scenario.reason`에 코드를 표시합니다. 같은 기기가 무선 디버깅으로 두 번 나열되어도(`ip:port`와 `adb-...._adb-tls-connect._tcp`) 하드웨어 일련번호(`ro.serialno`)가 같으면 한 대로 보고 `ip:port` 쪽을 자동 선택합니다. 일련번호가 다르면 이전처럼 `--device`가 필요합니다.

## 실패 분류(failure_class)

실패한 실행의 `result.json`에는 누가 조치해야 하는지 알려 주는 `failure_class`가 붙습니다. 분류표는 `camera_runner/failures.py`의 `CLASS_BY_CODE` 하나이며, 새 오류 코드는 여기에 추가합니다.

| `failure_class` | 의미 | 오류 코드 |
|---|---|---|
| `environment` | 단말·연결·권한 등 실행 환경 문제입니다. | `ADB_*`, `NO_DEVICE`, `DEVICE_*`, `PRECHECK_*`, `CAMERA_NOT_FOREGROUND`, `CAMERA_APP_NOT_FOUND`, `UI_UNAVAILABLE`, `IO_ERROR`, `VIDEO_COPY_INCOMPLETE`, `INTERRUPTED` |
| `automation` | 선택자, 프로필, 로그 패턴이 앱과 맞지 않습니다. 프로필을 고칩니다. | `MODE_NOT_FOUND`, `MODE_NOT_READY`, `INVALID_UI_BOUNDS`, `UNSUPPORTED_CAMERA_VERSION`, `INVALID_PROFILE_FILE`, `INVALID_CHECKS_FILE`, `INVALID_GOLDEN`, 그리고 `diagnosis.record_state_log`가 `lines > 0`, `matched == 0`인 `RECORD_START_UNVERIFIED` |
| `product` | 녹화, 종료, 저장이 실제로 실패했습니다. 앱 동작을 조사합니다. | `RECORD_START_UNVERIFIED`(로그 패턴 변경 증거 없음), `RECORDING_ENDED_EARLY`, `RECORD_STOP_UNVERIFIED`, `VIDEO_NOT_SAVED`, `AMBIGUOUS_VIDEO`, `CAMERA_LAUNCH_FAILED`, `CAMERA_PID_NOT_FOUND`, `CLEANUP_UNVERIFIED` |

표에 없는 코드는 `unknown`입니다.

## 화면 스냅샷과 골든 비교

PASS한 실행도 앱 UI가 바뀌었는지는 알려 주지 않습니다. 기준(골든) 화면과 비교하면 깨지기 전에 변화를 발견할 수 있습니다.

1. 정상 빌드에서 화면을 저장하며 실행합니다. `runs/<run-id>/screens/<checkpoint>.json`이 생깁니다. 체크포인트는 `initial`(인물 동영상이 아닌 모드에서 시작한 직후 화면), `initial_in_mode`(이미 인물 동영상 모드에서 시작한 직후 화면), `more_opened`(더보기를 연 경우), `mode_ready`(녹화 버튼이 준비된 화면)입니다. 녹화 중에는 UI를 덤프하지 않습니다.

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --capture-screens
   ```

2. PASS한 실행을 골든으로 승격합니다. 실패한 실행이나 `--capture-screens` 없이 실행한 폴더는 거부됩니다. 이미 있는 골든을 바꾸려면 `--force`를 씁니다.

   ```powershell
   python camera-test.pyz screens promote --run runs/<run-id> --output golden/17.0.00.43
   ```

3. 새 빌드에서 `--golden`으로 실행합니다. 결과에 `drift`가 붙지만 `scenario_status`와 종료 코드는 바뀌지 않습니다. 변화가 있으면 `warnings`에 `UI_DRIFT_BREAKING` 또는 `UI_DRIFT_COSMETIC`이 추가됩니다.

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --golden golden/17.0.00.43
   ```

4. 저장된 실행과 골든은 ADB 없이 다시 비교할 수 있습니다. 실행에 `--profile-file`을 썼다면 같은 파일을 지정합니다.

   ```powershell
   python camera-test.pyz screens compare --golden golden/17.0.00.43 --run runs/<run-id>
   ```

스냅샷에는 앱 패키지의 노드만, 그리고 `resource-id`(짧은 형태), `text`, `content-desc`, `class`만 저장합니다. 좌표와 다른 앱의 노드는 저장하지 않습니다. 체크포인트별 `missing_ids`, `new_ids`, `changed_labels`, `profile_selectors_missing`과 다음 심각도를 보고합니다.

| `severity` | 조건 |
|---|---|
| `breaking` | 프로필 선택자가 사라졌거나, 프로필 문구(`labels`)를 가진 노드의 문구가 바뀌었습니다. 프로필을 수정해야 할 가능성이 높습니다. |
| `cosmetic` | 위에 해당하지 않는 ID 추가·삭제나 문구 변경입니다. |
| `none` | 동일합니다. 골든에는 있으나 이번 실행이 지나가지 않은 체크포인트는 `not_reached: true`로 표시하고 `none`으로 칩니다(시작 상태가 달랐거나 실패한 경우이며, 실패는 실행 판정이 따로 알려 줍니다). |

갤럭시 카메라는 마지막 모드를 기억하므로 실행 후에는 인물 동영상 모드로 남습니다. `initial`과 `initial_in_mode`를 나눠 저장하는 이유이며, 같은 시작 상태의 화면끼리 비교할 때만 `initial` 비교가 의미 있습니다. 골든은 두 시작 상태에서 각각 만들어 두면 좋습니다.

## 녹화 영상 정리

`run --cleanup-video`는 검증(그리고 `--pull-video` 복사)이 끝난 뒤, 이번 실행이 만든 MP4의 정확한 경로(`video.device_path`) 하나만 단말에서 삭제하고 `video.deleted`에 결과를 기록합니다. 와일드카드는 쓰지 않습니다. 삭제에 실패하면 `warnings`에 `VIDEO_CLEANUP_FAILED`를 추가할 뿐 실행 결과는 바꾸지 않습니다. 파일 시스템에서 직접 지우므로 갤러리 색인에는 잠시 항목이 남을 수 있습니다.

## Qualcomm과 Exynos의 HAL 검사 등록

`hal begin`과 `hal collect`로 기능 OFF·ON 각각의 로그를 저장하고, `hal compare`로 태그별 차이 또는 적용 패턴을 확인합니다. `hal compare`는 저장된 `threadtime` 로그만 사용합니다. [ON/OFF 로그 수집 및 검사 작성 절차](docs/exynos-hal-validation.md)

[checks.example.json](checks.example.json)의 예시 태그와 패턴을 팀 빌드에서 실제 동작을 보여 주는 값으로 바꿉니다. 동일한 검사 이름에 `vendor`를 `exynos` 또는 `qualcomm`으로 지정해 별도 정의를 등록할 수 있습니다. 앱 로그 검사는 `source`를 `app`으로 지정하고 `vendor` 필드를 생략합니다.

```powershell
python camera_test.py inspect --pretty --checks-file checks.json
python camera_test.py run galaxy.portrait-video --check my-feature --checks-file checks.json
```

검사 시점에 활성화해야 하는 기능은 사내 개발 절차로 먼저 설정합니다. 판정에는 녹화 버튼을 누르기 직전부터 수집한 해당 태그의 메시지를 사용합니다. 정의 미등록, SoC 불일치, 예상 로그 미검출은 각각 `feature.reason`에 표시되며, 기능 상태는 `UNVERIFIED`입니다. `scenario_status`는 녹화 시나리오의 결과를 별도로 나타냅니다.

| `run` 종료 코드 | 의미 |
|---|---|
| `0` | 요청한 검증이 모두 통과했습니다. |
| `1` | 시나리오 또는 증거 수집 중 오류가 발생했습니다. |
| `2` | 시나리오는 통과했고 요청한 기능의 판정 근거가 없습니다. 잘못된 CLI 인자에도 `2`가 반환되며 이 경우 표준 오류에 사용법이 표시됩니다. |

`recording_may_be_active`가 `true`이면 단말에서 녹화 상태를 확인한 뒤 다시 실행합니다. `runs/`에는 단말 화면과 로그가 포함될 수 있어 Git에서 제외됩니다.

## Agent와 배포

스킬은 `SKILLS/`에 모여 있으며, Agent는 `AGENTS.md`에서 작업에 맞는 스킬 경로를 찾을 수 있습니다. Agent는 저장소의 CLI를 셸에서 호출합니다. Agent가 원격에서 실행될 경우, 실제 명령을 단말이 연결된 PC에서 실행하는 사내 도구에 연결합니다.

| 스킬 | 쓰는 경우 |
|---|---|
| [camera-test](SKILLS/camera-test/SKILL.md) | 지원 모드를 확인하고 등록된 시나리오를 실행합니다. |
| [camera-ui-diagnostics](SKILLS/camera-ui-diagnostics/SKILL.md) | 새 앱이나 모드를 조사하고, UI 변경으로 깨진 선택자를 수정합니다. |
| [android-cli](SKILLS/android-cli/SKILL.md) | Android SDK와 기기 상태를 조사할 때 참고합니다. 원본과 라이선스를 함께 제공합니다. [Android 공식 소스](https://github.com/android/skills/tree/c72942584fff970dac86ced78b367fce0c441e52/devtools/android-cli) · [Apache 2.0 라이선스](SKILLS/android-cli/LICENSE.txt) |

저장소 루트의 `camera-test.pyz`는 표준 라이브러리만 사용합니다. 소스에서 실행하려면 `python camera_test.py`에 같은 하위 명령을 전달합니다. 소스를 수정한 뒤에는 아래 명령으로 함께 배포하는 실행 파일을 갱신할 수 있습니다.

```powershell
python build.py camera-test.pyz
python camera-test.pyz inspect --pretty
```

## 저장소 구조

- `camera_test.py`와 `camera-test.pyz`는 저장소 루트에서 실행합니다.
- `camera_runner/adb.py`는 기기 선택과 ADB 통신, UI 덤프, 로그 수집을 담당합니다.
- `camera_runner/scenarios/`는 앱별 촬영과 판정을 담당합니다. 이 폴더의 등록 목록을 `list`, `inspect`, `run`이 함께 사용합니다. 새 시나리오는 구현을 추가하고 `camera_runner/scenarios/__init__.py`에 등록합니다.
- `camera_runner/preflight.py`, `camera_runner/failures.py`, `camera_runner/screens.py`는 각각 실행 전 환경 점검, 실패 분류표, 화면 스냅샷·골든 비교를 담당합니다.
- `camera_runner/checks.py`와 `camera_runner/hal.py`는 각각 검사 정의와 HAL 로그 수집·비교를 담당합니다.
