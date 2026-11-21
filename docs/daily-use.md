# 일상 검증과 결과 읽기

이 문서는 `run`으로 카메라 동작을 한 번 검증하고, 결과를 읽고, 문제가 생겼을 때 대응하는 방법을 설명합니다. 단말 연결은 [설치와 단말 준비](setup.md)를 먼저 마칩니다.

## 기본 실행

```powershell
python camera-test.pyz run galaxy.portrait-video --duration 5 --verify-media --cleanup-video
```

실행 순서는 다음과 같습니다.

```
실행 전 점검(화면·잠금·저장 공간)
  → 카메라 실행 → 인물 동영상 모드 진입(필요하면 더보기)
  → 녹화 시작(로그로 RECORDING 확인) → 요청한 시간 대기
  → 녹화 종료(로그로 STOPPING -> IDLE 확인) → 새 MP4 저장 확인
  → (--verify-media) MP4 구조 판정 → (--cleanup-video) 단말의 MP4 삭제
```

## 자주 쓰는 옵션

| 옵션 | 쓰는 경우 |
|---|---|
| `--duration N` | 녹화 시간(1~60초, 기본 5초)입니다. |
| `--verify-media` | 영상 파일을 PC로 복사해 영상 트랙, 프레임 수, 길이를 판정합니다. 권장합니다. |
| `--cleanup-video` | 이번 실행이 만든 MP4 하나만 단말에서 지웁니다. 반복 실행할 때 저장 공간을 지킵니다. |
| `--pull-video` | 영상을 `runs/<run-id>/portrait-video.mp4`로 받아 둡니다. |
| `--device <serial>` | 단말이 여러 대 연결되어 있을 때 지정합니다. |
| `--require-idle` | 카메라나 홈 화면이 아닌 앱이 떠 있으면 실행하지 않습니다. 남이 쓰는 단말일 수 있을 때 씁니다. |
| `--golden <dir>` | 기준 화면과 UI 변화를 비교합니다. [새 앱 빌드 대응](new-app-build.md#기준-화면golden으로-ui-변화-미리-보기)을 참고합니다. |

## 결과 읽기

명령은 JSON 한 줄을 출력하고, 같은 내용을 `runs/<run-id>/result.json`에 저장합니다. 먼저 아래 순서로 봅니다.

1. **`scenario_status`**: `PASS` 또는 `FAIL`입니다.
2. **`error.code`와 `failure_class`**: 실패했을 때 원인과 조치할 사람입니다.
3. **`warnings`**: 판정은 바꾸지 않는 주의 사항입니다. 예를 들어 `UNVERIFIED_CAMERA_VERSION`, `UI_DRIFT_BREAKING`, `MEDIA_NO_AUDIO`가 있습니다.
4. **`metrics`**: 녹화 시작·종료 지연과 저장 완료 시간(ms)입니다.
5. **`media`**: 코덱, 해상도, fps, 길이, 검사별 결과입니다.

| 종료 코드 | 뜻 |
|---|---|
| `0` | 요청한 검증이 모두 통과했습니다. |
| `1` | 시나리오, 영상 판정 또는 증거 수집이 실패했습니다. |
| `2` | 시나리오는 통과했지만 `--check`로 요청한 기능의 판정 근거가 없습니다. 잘못된 인자일 때도 `2`이며, 이때는 표준 오류에 사용법이 나옵니다. |

### 실행 폴더 `runs/<run-id>/`

| 파일 | 내용 |
|---|---|
| `result.json` | 결과 전체입니다. |
| `logcat.txt` | 실행 구간의 카메라 관련 로그입니다. |
| `failure.png`, `failure-ui.xml` | 화면 조작 중 실패했을 때의 화면과 UI 구조입니다. |
| `screens/` | `--capture-screens` 또는 `--golden`을 쓴 경우의 UI 스냅샷입니다. |
| `portrait-video.mp4` | `--pull-video`를 썼거나, 영상 판정에 실패해 증거로 남긴 영상입니다. |

### 참고 측정값

`SM-S936N`, 카메라 `17.0.00.43`, 무선 ADB에서 측정한 정상 범위입니다. 같은 단말과 연결 방식에서 이 값보다 크게 늘면 성능 변화를 의심할 수 있습니다.

| 항목 | 정상 범위 |
|---|---|
| 녹화 시작 지연(`record_start_latency_ms`) | 약 215~320ms |
| 녹화 종료 지연(`record_stop_latency_ms`) | 약 1,420~1,480ms |
| 저장 완료(`video_ready_ms`) | 약 2,200~2,550ms |
| 5초 요청 시 영상 길이 | 약 4.93~5.17초 (HEVC 1920x1080, 약 29.5fps, AAC) |

## 자주 만나는 문제

| 증상 또는 오류 코드 | 원인 | 조치 |
|---|---|---|
| `NO_DEVICE` | ADB에 연결된 단말이 없습니다. | `adb devices -l`로 확인합니다. 무선 디버깅은 포트가 바뀌었을 수 있으니 `adb connect`를 다시 합니다. |
| `DEVICE_REQUIRED` | 서로 다른 단말이 여러 대 연결되어 있습니다. | `--device <serial>`을 지정합니다. |
| `DEVICE_BUSY` | 같은 단말에서 다른 camera-test가 실행 중입니다. | 앞선 실행이 끝난 뒤 다시 실행합니다. |
| `PRECHECK_SCREEN_OFF` | 화면이 꺼져 있거나 절전(`Dozing`) 상태입니다. | 화면을 켭니다. 랩 단말은 [화면 유지 설정](lab-automation.md#1-랩-단말-설정)을 합니다. |
| `PRECHECK_LOCKED` | 잠금 화면입니다. | 잠금을 해제합니다. |
| `PRECHECK_LOW_STORAGE` | 여유 공간이 500MB 미만입니다. | 단말의 `DCIM/Camera`를 정리합니다. 반복 실행에는 `--cleanup-video`를 씁니다. |
| `PRECHECK_DEVICE_BUSY` | 카메라나 홈 화면이 아닌 앱이 떠 있습니다. | `result.json`의 `preflight.foreground.package`에서 어떤 앱인지 확인합니다. 단말을 쓰는 사람이 있으면 실행하지 않습니다. |
| `UI_UNAVAILABLE` | UI 구조를 읽지 못했습니다. 다른 앱이 앞에 있거나 화면이 잠겼을 때 주로 생깁니다. | 단말 화면을 확인한 뒤 다시 실행합니다. 모드 전환 직후의 일시적 실패는 자동으로 재시도하며, 그 횟수가 `ui_retries`에 남습니다. |
| `UNSUPPORTED_CAMERA_VERSION` | 카메라 앱 버전이 검증 목록에 없습니다. | [새 앱 빌드 대응](new-app-build.md)을 따릅니다. |
| `MODE_NOT_FOUND`, `MODE_NOT_READY` | 앱 UI가 바뀌어 모드나 녹화 버튼을 찾지 못했습니다. | [프로필 고치기](new-app-build.md#실행이-깨졌을-때-프로필-고치기)를 따릅니다. |
| `MEDIA_*` | 영상 구조나 길이가 잘못되었습니다. | 앱 결함으로 보고합니다. 영상은 `artifacts.video`에, 단말 원본은 그대로 남아 있습니다. |
| `recording_may_be_active: true` | 녹화 종료를 확인하지 못했습니다. | 단말에서 녹화가 멈췄는지 확인한 뒤 다시 실행합니다. |
| 프로필 JSON의 한글이 깨짐 | PowerShell 5.1의 `>`가 파일을 UTF-16으로 저장합니다. | `profile show ... --output <파일>`을 씁니다. |

오류 코드 전체 목록은 [참조](reference.md#실패-분류)에 있습니다.
