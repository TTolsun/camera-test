# 카메라 실기기 검증 도구

ADB로 연결한 갤럭시 단말에서 카메라 기능을 자동으로 재현하고 판정합니다. 개발자, QA, 사내 AI Agent가 같은 명령을 쓰며, 결과는 JSON과 증거 파일(로그, 화면, 영상)로 남습니다.

- **검증 대상**: 갤럭시 카메라 인물 동영상 녹화(`galaxy.portrait-video`)입니다. 모드 진입, 녹화 시작·종료, MP4 저장과 영상 구조를 판정하고 지연 시간을 측정합니다.
- **검증된 환경**: `SM-S936N`, Android 17, 갤럭시 카메라 `17.0.00.43`, 한국어 UI입니다.
- **필요한 것**: Python 3.10 이상, [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)(ADB), 개발자 옵션을 켠 테스트 단말입니다.

## 시작하기

1. 저장소를 받습니다. 별도 패키지 설치는 필요하지 않습니다.

   ```powershell
   git clone https://github.com/TTolsun/camera-test.git
   cd camera-test
   ```

2. 단말을 연결합니다.
   - **USB**: 케이블을 꽂고 단말에 뜨는 "USB 디버깅 허용"을 승인합니다.
   - **무선**: 단말의 개발자 옵션 > 무선 디버깅 > **페어링 코드로 기기 페어링**을 누르고, 표시된 주소로 `adb pair <IP>:<포트>`를 실행해 6자리 코드를 입력합니다. 이후 연결 명령은 다음 단계의 `doctor`가 알려 줍니다.

3. 환경을 점검합니다. 화면을 조작하지 않는 읽기 전용 점검이며, 실패한 항목마다 해결 방법을 출력합니다.

   ```powershell
   python camera-test.pyz doctor
   ```

   `결과: run을 실행할 준비가 되었습니다.`가 나올 때까지 `[FAIL]` 항목 아래의 `->` 안내를 따릅니다. 준비가 안 되면 `doctor`의 종료 코드는 `1`입니다.

4. 인물 동영상을 5초 녹화하고 판정합니다. 1회에 40~60초 걸리며, 그동안 단말 화면이 켜져 있고 잠금이 풀린 상태여야 합니다. 도구가 화면을 누르므로 단말을 만지지 않습니다.

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --verify-media --cleanup-video
   ```

## 결과 읽기

`run`은 JSON 한 줄을 출력하고, 같은 내용과 증거 파일을 `runs/<run-id>/`에 저장합니다.

| 필드 | 읽는 법 |
|---|---|
| `scenario_status` | `PASS`이면 성공입니다. 종료 코드도 `0`입니다. |
| `next_action` | 실패했을 때 다음에 할 일입니다. **먼저 이것을 따릅니다.** |
| `failure_class` | 누가 조치하는지 알려 줍니다. 아래 표를 봅니다. |
| `warnings` | 판정은 바꾸지 않는 주의 사항입니다. |
| `metrics`, `media` | 녹화 지연 시간(ms)과 영상 정보(코덱, 해상도, fps, 길이)입니다. |

| `failure_class` | 뜻 | 담당 |
|---|---|---|
| `environment` | 단말 상태나 연결 문제입니다. | 실행한 사람이 단말을 고치고 다시 실행합니다. |
| `automation` | 앱 UI나 로그가 바뀌어 자동화가 맞지 않습니다. | 도구 담당이 프로필을 고칩니다. [운영 가이드](docs/operations.md#프로필-고치기) |
| `product` | 녹화, 저장, 영상이 실제로 잘못되었습니다. | 앱 결함으로 보고합니다. `next_action`이 첨부할 파일을 알려 줍니다. |

## 자주 쓰는 옵션

| 옵션 | 쓰는 경우 |
|---|---|
| `--duration N` | 녹화 시간(1~60초, 기본 5초)입니다. |
| `--verify-media` | 영상의 트랙, 프레임 수, 길이를 판정합니다. 권장합니다. |
| `--cleanup-video` | 이번 실행이 만든 MP4 하나만 단말에서 지웁니다. |
| `--pull-video` | 영상을 `runs/<run-id>/`로 받아 둡니다. |
| `--device <serial>` | 단말이 여러 대일 때 지정합니다. |

모든 옵션은 `python camera-test.pyz run --help`에서, 결과 필드와 오류 코드 전체는 [참조](docs/reference.md)에서 확인합니다.

## 상황별 안내

| 상황 | 할 일 |
|---|---|
| 새 카메라 앱 빌드가 설치되었습니다. | `verify-build`로 반복 검증합니다. [운영 가이드](docs/operations.md#새-앱-빌드-검증) |
| 전용 랩 단말로 새 빌드를 자동 검증하려 합니다. | [운영 가이드](docs/operations.md#랩-단말-자동-운영) |
| 기능이 Camera HAL에 적용됐는지 로그로 확인하려 합니다. | [HAL 검사 만들기](docs/exynos-hal-validation.md) |

## 안전 수칙

- **테스트 단말에서만 실행합니다.** 도구는 화면을 누르고 녹화합니다. 개인 단말이나 다른 사람이 쓰는 단말에서는 실행하지 않습니다.
- **`runs/`는 커밋하지 않습니다.** 단말 화면, 로그, 영상이 들어 있어 Git에서 제외되어 있습니다.

## Agent에서 사용하기

사내 AI Agent는 [AGENTS.md](AGENTS.md)에서 작업에 맞는 스킬을 찾아 이 CLI를 셸에서 호출합니다. 환경을 모를 때는 `doctor --json`으로 시작하고, 결과의 `next_action`을 사용자에게 그대로 전달합니다.

| 스킬 | 쓰는 경우 |
|---|---|
| [camera-test](SKILLS/camera-test/SKILL.md) | 환경 점검, 시나리오 실행, 새 빌드 검증 |
| [camera-ui-diagnostics](SKILLS/camera-ui-diagnostics/SKILL.md) | 새 앱·모드 조사, UI 변경으로 깨진 프로필 수정 |
| [android-cli](SKILLS/android-cli/SKILL.md) | Android SDK와 기기 상태 조사 ([원본](https://github.com/android/skills/tree/c72942584fff970dac86ced78b367fce0c441e52/devtools/android-cli) · [Apache 2.0](SKILLS/android-cli/LICENSE.txt)) |

## 개발자용

- 소스에서 실행하려면 `python camera_test.py`에 같은 하위 명령을 전달합니다.
- 테스트는 실기기 없이 실행됩니다: `python -m unittest discover -s tests`
- `camera_runner/`를 수정하면 `python build.py camera-test.pyz`로 배포 파일을 다시 만들어 함께 커밋합니다.
- 오류 코드를 추가하면 `camera_runner/guidance.py`에 안내 문구도 추가합니다. 빠뜨리면 테스트가 실패합니다.
- 구조와 설계는 [CLAUDE.md](CLAUDE.md)에 있습니다.
