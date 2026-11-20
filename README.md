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

카메라 앱 프로필은 `SM-S936N`, Android 17, Qualcomm `SM8750`, 갤럭시 카메라 `17.0.00.43`의 한국어 환경에서 검증했습니다. 다른 카메라 앱 버전에서는 조작 전에 중단합니다. Exynos 실기기 검증에는 팀에서 사용하는 앱 빌드와 Camera HAL 로그 정의가 필요합니다.

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
- `camera_runner/checks.py`와 `camera_runner/hal.py`는 각각 검사 정의와 HAL 로그 수집·비교를 담당합니다.
