# 카메라 실기기 검증 도구

ADB로 연결한 갤럭시 단말에서 카메라 기능을 자동으로 재현하고 판정합니다. 개발자, QA, 사내 AI Agent가 같은 명령을 사용하며, 결과는 JSON과 증거 파일(로그, 화면, 영상)로 남습니다.

- **지금 할 수 있는 검증**: 갤럭시 카메라의 인물 동영상 녹화(`galaxy.portrait-video`)입니다. 모드 진입, 녹화 시작·종료, MP4 저장, 영상 구조(트랙, 프레임, 길이)를 판정하고 지연 시간을 측정합니다.
- **검증된 환경**: `SM-S936N`, Android 17, Qualcomm `SM8750`, 갤럭시 카메라 `17.0.00.43`, 한국어 UI입니다.
- **필요한 것**: Python 3.10 이상, [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)(ADB), 테스트 단말입니다. 그 밖의 설치는 필요하지 않습니다.

## 5분 안에 첫 실행하기

1. 저장소를 받습니다.

   ```powershell
   git clone https://github.com/TTolsun/camera-test.git
   cd camera-test
   ```

2. 단말을 USB 또는 무선 디버깅으로 연결하고, 잠금을 해제한 뒤 화면을 켜 둡니다. 연결 방법은 [설치와 단말 준비](docs/setup.md)에 있습니다.

   ```powershell
   adb devices -l
   ```

3. 단말과 카메라 상태를 확인합니다. `scenario.status`가 `runnable`이면 실행할 수 있습니다.

   ```powershell
   python camera-test.pyz inspect --pretty
   ```

4. 인물 동영상을 5초 녹화하고 판정합니다. 1회에 40~60초 정도 걸립니다.

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --duration 5 --verify-media --cleanup-video
   ```

5. 출력 JSON에서 `scenario_status`가 `PASS`이고 종료 코드가 `0`이면 성공입니다. 실패하면 `failure_class`를 보고 [실패했을 때](#실패했을-때-누가-무엇을-하나)를 따릅니다. 증거 파일은 `runs/<run-id>/`에 저장됩니다.

> `--cleanup-video`는 이번 실행이 만든 MP4 하나만 단말에서 지웁니다. 영상을 PC로 받고 싶으면 `--pull-video`를 추가합니다.

## 상황별 사용법

| 상황 | 쓰는 명령 | 가이드 |
|---|---|---|
| 단말에서 카메라 동작을 한 번 확인합니다. | `run` | [일상 검증과 결과 읽기](docs/daily-use.md) |
| 새 카메라 앱 빌드가 설치되었습니다. | `verify-build` | [새 앱 빌드 대응](docs/new-app-build.md) |
| 새 빌드에서 실행이 깨졌습니다(`needs_profile_fix`). | `profile`, `screens` | [새 앱 빌드 대응](docs/new-app-build.md#실행이-깨졌을-때-프로필-고치기) |
| 전용 랩 단말로 새 빌드를 매시간 자동 검증합니다. | `verify-build --if-changed` | [랩 단말 자동 운영](docs/lab-automation.md) |
| 기능이 Camera HAL에 실제로 적용됐는지 로그로 확인합니다. | `hal`, `run --check` | [HAL 검사 만들기](docs/exynos-hal-validation.md) |
| 결과 필드, 오류 코드, 임계값을 찾아봅니다. | | [참조](docs/reference.md) |

## 실패했을 때 누가 무엇을 하나

실패한 결과에는 `failure_class`가 붙습니다. 이 값으로 조치할 사람을 정합니다.

| `failure_class` | 뜻 | 할 일 |
|---|---|---|
| `environment` | 단말 상태나 연결 문제입니다. 앱 문제가 아닙니다. | 화면 켜짐, 잠금 해제, 저장 공간, 연결을 확인하고 다시 실행합니다. [문제 해결](docs/daily-use.md#자주-만나는-문제) |
| `automation` | 앱 UI나 로그가 바뀌어 자동화가 맞지 않습니다. | 프로필을 고칩니다. 코드 수정은 대부분 필요하지 않습니다. [프로필 고치기](docs/new-app-build.md#실행이-깨졌을-때-프로필-고치기) |
| `product` | 녹화, 종료, 저장, 영상 구조가 실제로 잘못되었습니다. | 앱 결함으로 보고 `runs/<run-id>/`의 `logcat.txt`, `failure.png`, 영상을 첨부해 보고합니다. |

## 안전 수칙

- **테스트 단말에서만 실행합니다.** 도구는 화면을 누르고 녹화합니다. 개인 단말이나 다른 사람이 쓰는 단말에서는 실행하지 않습니다.
- **무인 실행은 전용 랩 단말에만 등록합니다.** `verify-build`는 카메라나 홈 화면이 아닌 앱이 떠 있으면 실행하지 않습니다(`PRECHECK_DEVICE_BUSY`). 그래도 모든 상황을 막지는 못합니다.
- **`runs/`는 커밋하지 않습니다.** 단말 화면, 로그, 영상이 들어 있어 Git에서 제외되어 있습니다.

## Agent에서 사용하기

사내 AI Agent는 [AGENTS.md](AGENTS.md)에서 작업에 맞는 스킬을 찾아 이 CLI를 셸에서 호출합니다. Agent가 원격에서 실행되면, 단말이 연결된 PC에서 명령을 실행하는 사내 도구에 연결합니다.

| 스킬 | 쓰는 경우 |
|---|---|
| [camera-test](SKILLS/camera-test/SKILL.md) | 모드를 확인하고, 시나리오를 실행하고, 새 빌드를 검증합니다. |
| [camera-ui-diagnostics](SKILLS/camera-ui-diagnostics/SKILL.md) | 새 앱이나 모드를 조사하고, UI 변경으로 깨진 프로필을 고칩니다. |
| [android-cli](SKILLS/android-cli/SKILL.md) | Android SDK와 기기 상태를 조사합니다. [Android 공식 소스](https://github.com/android/skills/tree/c72942584fff970dac86ced78b367fce0c441e52/devtools/android-cli) · [Apache 2.0 라이선스](SKILLS/android-cli/LICENSE.txt) |

## 개발자용

- 소스에서 실행하려면 `python camera_test.py`에 같은 하위 명령을 전달합니다.
- 테스트는 실기기 없이 실행됩니다: `python -m unittest discover -s tests`
- `camera_runner/`를 수정하면 `python build.py camera-test.pyz`로 배포 파일을 다시 만들어 함께 커밋합니다.
- 구조와 설계는 [CLAUDE.md](CLAUDE.md)의 아키텍처 절에 있습니다.
