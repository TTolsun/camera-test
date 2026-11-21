# 참조

명령, 결과 필드, 오류 코드, 임계값을 찾아보는 문서입니다. 사용 절차는 [README](../README.md)와 [운영 가이드](operations.md)에 있습니다. 실패했을 때 할 일은 결과의 `next_action`이 알려 주므로, 이 문서는 필드의 뜻을 찾아볼 때만 씁니다.

## 명령 목록

| 명령 | ADB 필요 | 설명 |
|---|---|---|
| `doctor` | 예(읽기 전용) | Python, ADB, 단말 연결, 카메라 앱 버전, 화면·잠금·저장 공간을 점검하고 항목별 해결 방법을 출력합니다. 화면을 조작하지 않습니다. `--json`은 Agent용이며, 준비되지 않았으면 종료 코드가 `1`입니다. |
| `list` | 아니요 | 등록된 시나리오 이름을 출력합니다. |
| `inspect` | 예 | 단말, SoC, 카메라 앱 버전, 관찰된 모드, 시나리오 상태를 확인합니다. `--no-ui`는 카메라를 열지 않습니다. |
| `run <scenario>` | 예 | 시나리오를 한 번 실행하고 판정합니다. |
| `verify-build <scenario>` | 예 | 시나리오를 반복 실행해 집계하고 결정을 내립니다. |
| `profile show <scenario>` | 아니요 | 내장 프로필을 JSON으로 출력합니다. `--output`으로 UTF-8 파일에 씁니다. |
| `profile validate <path>` | 아니요 | 프로필 JSON을 검사합니다. |
| `screens promote` | 아니요 | 통과한 실행의 화면을 기준 화면으로 만듭니다. `--merge`, `--force`를 지원합니다. |
| `screens compare` | 아니요 | 기준 화면과 저장된 실행을 비교합니다. |
| `media inspect <mp4>` | 아니요 | MP4의 브랜드, 길이, 트랙 정보를 출력합니다. |
| `hal begin/collect/compare` | 비교만 아니요 | Camera HAL 로그를 기능 OFF·ON으로 수집하고 비교합니다. |

## run 옵션

| 옵션 | 설명 |
|---|---|
| `--device` | ADB 일련번호입니다. 실기기가 한 대이면 생략할 수 있습니다. |
| `--duration` | 녹화 초(1~60, 기본 5)입니다. |
| `--output-dir` | 실행 폴더의 상위 폴더(기본 `runs`)입니다. |
| `--pull-video` | 새 MP4를 실행 폴더로 복사합니다. |
| `--verify-media` | MP4를 복사해 구조와 길이를 판정합니다. |
| `--cleanup-video` | 판정이 끝난 뒤 이번 실행이 만든 MP4 하나만 단말에서 지웁니다. 영상 판정이 실패하면 지우지 않습니다. |
| `--capture-screens` | 체크포인트별 UI 스냅샷을 `screens/`에 저장합니다. |
| `--golden` | 기준 화면과 비교해 `drift`를 붙입니다. `--capture-screens`를 포함합니다. |
| `--profile-file` | 내장 프로필 대신 쓸 프로필 JSON입니다. |
| `--allow-unverified-version` | 앱 버전이 `verified_versions`에 없어도 실행합니다. 결과에 `UNVERIFIED_CAMERA_VERSION` 경고가 붙습니다. |
| `--require-idle` | 카메라나 홈 화면이 아닌 앱이 떠 있으면 `PRECHECK_DEVICE_BUSY`로 중단합니다. |
| `--check`, `--checks-file` | 등록된 기능 검사를 함께 판정합니다. [HAL 검사](#hal과-앱-로그-검사)를 참고합니다. |

## 실행 전 점검(preflight)

화면을 조작하기 전에 점검하고, 측정값을 `result.preflight`에 기록합니다. 점검에 실패해도 측정값은 남습니다. 신호를 읽지 못한 항목은 `unknown`으로 기록하며 실패로 처리하지 않습니다.

| 오류 코드 | 조건 |
|---|---|
| `PRECHECK_SCREEN_OFF` | `dumpsys power`의 `mWakefulness`가 `Awake`가 아닙니다(`Asleep`, `Dozing`). |
| `PRECHECK_LOCKED` | `dumpsys window`의 `isKeyguardShowing=true`입니다(구형 Android는 `mShowingLockscreen=true`). |
| `PRECHECK_LOW_STORAGE` | `/sdcard`의 사용 가능 공간이 500MB(`preflight.py`의 `MIN_FREE_BYTES`) 미만입니다. |
| `PRECHECK_DEVICE_BUSY` | `--require-idle` 또는 `verify-build`일 때만 점검합니다. 최상단 앱이 카메라도 홈 런처도 아닙니다. |

## 실패 분류

분류표는 `camera_runner/failures.py`의 `CLASS_BY_CODE` 하나이며, 새 오류 코드는 여기에 추가합니다. 표에 없는 코드는 `unknown`입니다.

| `failure_class` | 오류 코드 |
|---|---|
| `environment` | `ADB_*`, `NO_DEVICE`, `DEVICE_*`, `PRECHECK_*`, `CAMERA_NOT_FOREGROUND`, `CAMERA_APP_NOT_FOUND`, `UI_UNAVAILABLE`, `IO_ERROR`, `RESULT_WRITE_FAILED`, `VIDEO_COPY_INCOMPLETE`, `INTERRUPTED` |
| `automation` | `MODE_NOT_FOUND`, `MODE_NOT_READY`, `INVALID_UI_BOUNDS`, `UNSUPPORTED_CAMERA_VERSION`, `INVALID_PROFILE_FILE`, `INVALID_CHECKS_FILE`, `INVALID_GOLDEN`, 그리고 로그 패턴 변경 증거(`record_state_log`의 `lines > 0`, `matched == 0`)가 있는 `RECORD_START_UNVERIFIED` |
| `product` | `RECORD_START_UNVERIFIED`(로그 패턴 변경 증거 없음), `RECORDING_ENDED_EARLY`, `RECORD_STOP_UNVERIFIED`, `VIDEO_NOT_SAVED`, `AMBIGUOUS_VIDEO`, `CAMERA_LAUNCH_FAILED`, `CAMERA_PID_NOT_FOUND`, `CLEANUP_UNVERIFIED`, `MEDIA_NO_MOOV`, `MEDIA_MALFORMED`, `MEDIA_NO_VIDEO`, `MEDIA_NO_FRAMES`, `MEDIA_DURATION` |

## 경고(warnings)

경고는 판정과 종료 코드를 바꾸지 않습니다.

| 경고 | 뜻 |
|---|---|
| `UNVERIFIED_CAMERA_VERSION` | `--allow-unverified-version`으로 검증 목록에 없는 버전을 실행했습니다. |
| `UI_DRIFT_BREAKING` | 기준 화면 대비 프로필 선택자나 프로필 문구가 바뀌었습니다. |
| `MEDIA_NO_AUDIO` | 영상에 오디오 트랙이 없습니다. 카메라 설정에서 소리를 끌 수 있으므로 실패로 보지 않습니다. |
| `VIDEO_CLEANUP_FAILED` | `--cleanup-video`가 단말의 MP4를 지우지 못했습니다. |

## 영상 판정(--verify-media)

MP4를 표준 라이브러리로 파싱합니다. `mdat`는 건너뛰므로 큰 파일도 메모리에 전부 올리지 않습니다. 결과는 `result.media`에 `brand`, `duration_s`, `tracks`(`type`, `codec`, `duration_s`, `width`, `height`, `sample_count`, `fps`), `checks`로 기록됩니다.

| 검사 | 오류 코드 | 조건 |
|---|---|---|
| `video_track` | `MEDIA_NO_VIDEO` | 비디오 트랙이 없습니다. |
| `frames` | `MEDIA_NO_FRAMES` | 비디오 샘플이 0개입니다. |
| `duration` | `MEDIA_DURATION` | 길이가 `요청 초 − 0.5초` ~ `요청 초 + 2.0초` 밖입니다. |
| `audio` | `MEDIA_NO_AUDIO`(경고) | 오디오 트랙이 없습니다. |
| (파싱) | `MEDIA_NO_MOOV`, `MEDIA_MALFORMED` | `moov`가 없거나(끝까지 기록되지 않은 파일) 박스 구조가 깨졌습니다. |

길이 허용 범위는 `media.py`의 `DURATION_SHORT_TOLERANCE_S`, `DURATION_LONG_TOLERANCE_S`입니다. 녹화는 시작 탭 뒤에 시작하고 종료 탭을 처리한 뒤에 끝나며, 그 사이에 ADB 호출 시간이 들어가므로 긴 쪽 여유를 더 넓게 잡았습니다. 실기기에서 5초를 요청했을 때 4.93~5.17초였습니다. `width`, `height`는 회전을 적용하지 않은 `tkhd` 값입니다.

## 측정값(metrics)

추가 ADB 호출 없이 항상 기록합니다. 계산할 수 없는 값은 생략합니다.

| 키 | 측정 구간 |
|---|---|
| `record_start_latency_ms` | 시작 탭 직전의 기기 시각부터 `RECORDING` 전환 로그까지입니다. |
| `record_stop_latency_ms` | 종료 탭 직전의 기기 시각부터 `STOPPING -> IDLE` 로그까지입니다. |
| `video_ready_ms` | 종료 탭 직전부터 새 MP4의 크기가 안정될 때까지입니다. PC 시계로 재며 0.25초 간격 폴링이 포함됩니다. |

앞의 두 값에는 ADB 왕복 시간이 들어가므로, 빌드끼리 비교할 때는 같은 단말과 같은 연결 방식(USB 또는 무선)에서 측정합니다.

## 기준 화면 비교(drift)

스냅샷에는 카메라 앱 노드의 `resource-id`(짧은 형태), `text`, `content-desc`, `class`만 저장합니다. 좌표와 다른 앱의 노드는 저장하지 않습니다. 체크포인트별로 `missing_ids`, `new_ids`, `changed_labels`, `profile_selectors_missing`, `severity`를 보고합니다. 프로필의 `volatile_ids`는 비교에서 제외합니다.

| 체크포인트 | 저장 시점 |
|---|---|
| `initial` | 인물 동영상이 아닌 모드에서 시작한 직후입니다. |
| `initial_in_mode` | 이미 인물 동영상 모드에서 시작한 직후입니다. |
| `more_opened` | 더보기를 연 직후입니다. |
| `mode_ready` | 녹화 버튼이 준비된 화면입니다. 녹화 중에는 UI를 덤프하지 않습니다. |

## verify-build

### 옵션

| 옵션 | 설명 |
|---|---|
| `--repeat N` | 반복 횟수(1~50, 기본 5)입니다. |
| `--duration` | 녹화 초(기본 5)입니다. |
| `--profile-file` | 검증할 프로필 JSON입니다. 제안은 이 프로필에서 만듭니다. |
| `--golden` | 기준 화면 폴더입니다. |
| `--output-dir` | 배치 폴더의 상위 폴더(기본 `runs/verify`)입니다. |
| `--state-file` | 단말과 시나리오별 마지막 배치를 기록하는 JSON입니다. |
| `--if-changed` | 상태 파일에 같은 앱 버전의 기록이 있으면 단말을 건드리지 않고 `unchanged`로 끝납니다. `environment_blocked` 기록은 건너뛰지 않습니다. |
| `--baseline` | 이전 `report.json`과 지표 중앙값을 비교합니다. |

### 결정 순서와 종료 코드

위에서부터 먼저 맞는 조건이 결정이 됩니다.

| `decision` | 조건 | 종료 코드 |
|---|---|---|
| `environment_blocked` | `environment` 실패로 중단했습니다. | `3` |
| `needs_profile_fix` | `automation` 실패가 있거나, drift 최악 등급이 `breaking`입니다. | `1` |
| `product_failure` | `product` 실패가 있고 `automation` 실패는 없습니다. | `1` |
| `promote` | 검증 목록에 없는 버전이고, 전부 통과했고, `breaking` drift가 없습니다. | `0` |
| `baseline_ok` | 검증된 버전이고 전부 통과했습니다. | `0` |
| `flaky` | 위에 맞지 않는 통과·실패 혼합입니다. | `1` |
| `unchanged` | `--if-changed`로 건너뛰었습니다. | `0` |

`environment` 실패가 한 번 나오거나 `automation` 실패가 2회 연속 나오면 남은 반복을 건너뜁니다. 지표 회귀는 중앙값이 25%와 100ms를 모두 넘게 늘었을 때이며(`verify.py`의 `REGRESSION_RATIO`, `REGRESSION_MIN_MS`), `METRIC_REGRESSION` 경고만 추가합니다.

### 배치 폴더

| 파일 | 내용 |
|---|---|
| `report.json` | 앱 버전, 반복 수와 통과율, 실패 분류와 오류 코드 개수, `ui_retries` 합계, 지표별 최소·중앙값·최대(통과한 실행만), 영상 요약, drift 요약, 경고, 실행별 결과 경로, 결정입니다. |
| `report.md` | 결정, 핵심 숫자, 다음에 실행할 명령입니다. |
| `proposal/profile.json` | `promote`일 때만 생깁니다. `verified_versions`에 새 버전을 추가한 프로필입니다. |
| `proposal/golden/` | `promote`일 때만 생깁니다. 통과한 모든 반복의 체크포인트를 합친 기준 화면입니다. |
| `<run-id>/` | 반복마다의 실행 폴더입니다. |

### 상태 파일

```json
{"schema_version": 1, "devices": {"<ro.serialno>": {"galaxy.portrait-video": {"app_version": "...", "decision": "...", "report": "...", "finished": "..."}}}}
```

임시 파일에 쓴 뒤 교체하므로, 쓰는 도중에 중단되어도 파일이 깨지지 않습니다.

## HAL과 앱 로그 검사

`--checks-file`의 JSON(`schema_version: 1`)에 기능별 판정 로그를 등록하고, `run --check <name>`으로 녹화와 함께 판정합니다. 형식은 [checks.example.json](../checks.example.json)을 참고합니다.

- `source: hal`은 `vendor`(`exynos` 또는 `qualcomm`)가 필수입니다. 같은 이름에 vendor별 정의를 둘 수 있고, 감지된 SoC에 맞는 정의를 씁니다.
- `source: app`은 `vendor`를 생략합니다.
- 판정은 녹화 버튼을 누르기 직전부터 수집한 해당 태그의 로그에 정규식이 맞는지로 합니다.
- 정의 미등록, SoC 불일치, 예상 로그 미검출은 `feature.reason`에 표시되고, 기능 상태는 `UNVERIFIED`이며 종료 코드는 `2`입니다.

```powershell
python camera-test.pyz inspect --pretty --checks-file checks.json
python camera-test.pyz run galaxy.portrait-video --check my-feature --checks-file checks.json
```

로그 정의를 만드는 절차는 [HAL 검사 만들기](exynos-hal-validation.md)에 있습니다.
