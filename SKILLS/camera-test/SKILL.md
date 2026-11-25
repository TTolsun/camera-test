---
name: camera-test
description: Run the Galaxy camera mode the user names in Korean or English on an ADB device, including Pro Video, Portrait Video, Night, Photo and front camera requests. Also discover modes, run full suites and collect Camera HAL logs.
---

# 카메라 실기기 검증

저장소 루트에서 실행합니다. Agent가 원격이라면 ADB로 실기기에 연결된 PC에서 명령을 실행하는 사내 도구를 사용합니다.

### 특정 모드를 실행해 달라는 요청

1. 사용자 문장에서 모드명, 방향, 녹화 시간을 읽습니다. `python camera-test.pyz mode --list --json`으로 이름을 확인할 수 있습니다. “프로 동영상 / Pro Video”와 “인물 동영상 / Portrait Video”는 서로 다른 모드입니다.
2. 해당 단말을 조작할 수 있는 셸에서 `python camera-test.pyz mode "<모드명>" --register-device --json`을 실행합니다. “전면”이면 `--facing front`를, “8초”이면 `--duration 8`을 붙입니다. 방향을 생략한 요청은 후면입니다. 요청이 “야간 촬영해줘”로 바뀌면 같은 명령에 `"야간"`을 전달합니다.
3. 출력에서 실제 촬영 방향과 저장 결과를 확인해 사용자에게 전달합니다. 연결·잠금 등 실행 환경 문제는 `doctor --json`으로 진단합니다. 목록에 없는 모드는 단말에서 `inspect`로 관찰한 뒤 `camera-ui-diagnostics` 절차로 조사합니다. 수동 촬영 또는 외부 앱이 필요한 항목은 해당 상태를 알립니다.

### 모드 진입과 연속 요청

“열어줘”나 “모드로 전환해줘”처럼 화면 진입만 요청하면 `mode "<모드명>" --open-only --register-device --json`을 사용하고 방향을 함께 확인합니다. “동작시켜줘”는 사진 1장 또는 짧은 녹화 1회와 저장 확인까지 실행합니다.

여러 동작을 요청하면 모드·방향·시간·진입 또는 촬영 여부를 각 단계에서 먼저 식별합니다. 구분한 단계는 같은 단말에서 지정된 순서대로 실행합니다. 각 `mode` 명령의 종료 코드가 0이고 결과가 요청한 동작과 일치할 때 다음 단계로 넘어갑니다. 실패하면 완료된 단계, 멈춘 단계의 사유, 아직 실행하지 않은 단계를 알려 줍니다. 요청에 자동화되지 않은 설정이 포함되면 촬영을 시작하기 전에 해당 설정을 확인할 방법이나 대안을 사용자와 정합니다.

“Pro Video 전·후면을 각각 검사해서 등록해 줘”는 두 방향을 독립적으로 검사하는 요청입니다. 같은 단말에서 `--facing rear`와 `--facing front`로 각각 한 번 실행합니다. 첫 방향에서 `NOT_OBSERVED` 또는 `UNVERIFIED_FACING`이면 다음 방향도 검사합니다. 환경 오류, 등록 오류 또는 녹화 종료가 불확실한 경우에는 멈춥니다. 두 결과와 단말 등록부 경로를 함께 알려 줍니다.

### 단말 점검과 일괄 검사

0. 환경(ADB, 단말 연결, 카메라 앱 버전, 단말 상태)을 모르면 먼저 `python camera_test.py doctor --json`을 실행합니다. 읽기 전용이라 어떤 단말에서도 안전합니다. `ready`가 `false`이면 `next_action`(검사별 `next_action` 포함)을 **그대로 사용자에게 전달**하고 임의로 우회하지 않습니다. 도구가 출력하는 모든 `error.next_action`, 결과의 `next_action`, `scenario.next_action`, `verify-build` 요약의 `next_action`도 같은 방식으로 전달합니다.

전·후면 전체 검사와 등록에는 `python camera-test.pyz suite --register-device --json`을 실행합니다. 단말의 잠금을 풀고 카메라를 사진 모드에 둡니다. 같은 실기기와 앱 버전은 등록된 목록을 재사용하며 촬영은 매번 검증합니다. 목록을 다시 살피려면 `--rescan`을 추가합니다. `artifacts.report`와 `registry.path`를 전달하고 완료되지 않은 모드를 보고합니다. `recording_may_be_active`가 `true`이면 재실행 전에 단말의 녹화 상태를 확인합니다. [suite 참조](../../docs/reference.md#suite-옵션과-결과)

검증 이력만 묻는 요청에는 같은 작업 폴더에서 `python camera-test.pyz capabilities --json`을 사용합니다. 전·후면 각각의 목록 관찰, 화면 진입, 저장 성공과 최근 시도 결과를 구분합니다. 요청한 방향에서 모드를 찾지 못했거나 방향이 모호하면 그 결과를 그대로 전합니다.

1. `python camera_test.py inspect`로 기기, SoC, 관찰된 카메라 모드, 실행 가능한 시나리오를 확인합니다. 팀 검사 파일이 있으면 `--checks-file <path>`를 추가합니다. 특정 기기를 요청받았거나 여러 기기가 연결되어 있으면 `--device <serial>`을 지정합니다. 화면 탐색을 피해야 하는 요청에는 `inspect --no-ui`를 사용합니다.
2. `scenario.status`가 `runnable`이고 이름이 요청과 일치하면 `python camera_test.py run galaxy.portrait-video --duration 5`처럼 실행합니다. `unverified`이면 앱 버전이 프로필의 검증 목록에 없는 상태이므로, 사용자에게 보고하고 검증 실행을 요청받은 경우에만 `--allow-unverified-version`을 추가합니다. 실패 결과는 먼저 `failure_class`로 나눕니다. `environment`(`PRECHECK_*` 포함: 화면 꺼짐, 잠금, 저장 공간 부족)는 사용자에게 단말 상태를 고치도록 안내하고, `automation`은 `diagnosis`를 `camera-ui-diagnostics` 스킬로 분석하며, `product`는 앱 동작 문제로 로그와 함께 보고합니다. 발견된 모드와 등록된 시나리오가 다르면 관찰 결과와 지원 범위를 보고합니다. 빌드 배포나 기능 설정을 요청받았다면 해당 개발 프로젝트의 설정 절차를 먼저 적용합니다.
3. 특정 기능을 확인하려면 `--check <name>`과 팀 검사 파일 경로를 사용합니다. `checks.registered_for_device`에서 현재 SoC에 적용되는 정의를 확인합니다. 검사 파일은 `--checks-file <path>`로 전달합니다. 결과가 `UNVERIFIED`이면 `feature.reason`을 보고합니다. HAL 검사 정의 형식은 [참조 문서](../../docs/reference.md#hal과-앱-로그-검사)에 있습니다. 오류 코드와 결과 필드도 같은 문서에서 찾습니다.
4. 새 앱 빌드에서는 기준 화면과 비교하도록 `--golden <dir>`를 추가할 수 있습니다. 결과의 `drift`와 `warnings`의 `UI_DRIFT_BREAKING`은 판정(`scenario_status`)을 바꾸지 않으므로 별도로 보고합니다. 단말에 영상을 남기지 않으려면 `--cleanup-video`를 사용합니다. 녹화 파일 내용까지 판정하려면 `--verify-media`를 추가합니다(`result.media.checks`, 실패 코드 `MEDIA_*`는 `product`로 분류). 오디오가 없으면 `warnings`의 `MEDIA_NO_AUDIO`로만 알립니다. 이미 가진 MP4는 `python camera_test.py media inspect <path>`로 ADB 없이 확인하고, `result.metrics`의 지연 시간(`record_start_latency_ms`, `record_stop_latency_ms`, `video_ready_ms`)은 빌드 비교용으로 함께 전달합니다. JSON의 `scenario_status`와 `feature.status`를 각각 전달하고 실행 ID와 로그 경로를 알려 줍니다. 실패 분석에는 관련 로그 구간을 읽습니다. `recording_may_be_active`가 `true`이면 재실행 전에 단말의 녹화 상태를 확인합니다.

5. 새 앱 빌드가 단말에 올라왔는지 점검하려면 `run`이 아니라 `python camera_test.py verify-build galaxy.portrait-video --repeat 5 --golden <dir> --state-file <path>`를 사용합니다. `run`은 지금 한 번의 동작 확인이고, `verify-build`는 반복 실행·집계·결정을 만듭니다. 단말을 다른 사람이 쓰는 중일 수 있으므로 `verify-build`와 `run --require-idle`은 `PRECHECK_DEVICE_BUSY`로 멈출 수 있습니다. 이 경우 단말 주인에게 알려야 하는 상황이므로 사용자에게 보고하고 재시도는 사용자에게 맡깁니다. 예약 실행용 `--if-changed`는 사용자가 요청할 때만 씁니다. 출력의 `decision`(`promote`, `baseline_ok`, `needs_profile_fix`, `product_failure`, `flaky`, `environment_blocked`)과 `report` 경로를 **반드시 사용자에게 보고**합니다. `promote`의 `proposal/`(프로필·골든)은 사용자가 승인하기 전에는 내장 프로필(`camera_runner/apps/galaxy_camera.py`)에도 `camera-test.pyz`에도 적용하지 않습니다. `needs_profile_fix`는 `camera-ui-diagnostics` 스킬의 배치 보고 절차로 넘깁니다. 종료 코드는 `0`(promote, baseline_ok, unchanged), `1`(그 외 실패), `3`(환경)입니다.

Exynos용 HAL 검사 근거가 필요하면, 각 상태를 팀 절차로 적용한 뒤 `hal begin --state off|on`의 `trial` 경로를 보관합니다. 촬영 후 `hal collect --trial <path>`의 `logcat` 경로를 `hal compare --off <path> --on <path>`에 전달합니다. 새 검사 정의로 등록하기 전에 기능 적용 신호를 `--tag`와 `--expect-log`로 비교합니다. 양쪽 촬영이 기록됐는지는 `--control-tag`와 `--control-log`로 확인합니다. 인자와 예시는 [ON/OFF 로그 수집 절차](../../docs/exynos-hal-validation.md)에 있습니다.
