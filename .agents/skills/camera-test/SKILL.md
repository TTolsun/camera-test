---
name: camera-test
description: Discover available camera modes and run repeatable Galaxy camera checks on a connected Android device, including Portrait Video recording, saved video confirmation, and app or SoC-specific Camera HAL logs. Use when asked to verify camera behavior on hardware, exercise a camera mode, or collect logs for a camera change.
---

# 카메라 실기기 검증

저장소 루트에서 실행합니다. Agent가 원격이라면 ADB로 실기기에 연결된 PC에서 명령을 실행하는 사내 도구를 사용합니다.

1. `python camera_test.py inspect`로 기기, SoC, 관찰된 카메라 모드, 실행 가능한 시나리오를 확인합니다. 팀 검사 파일이 있으면 `--checks-file <path>`를 추가합니다. 특정 기기를 요청받았거나 여러 기기가 연결되어 있으면 `--device <serial>`을 지정합니다. 화면 탐색을 피해야 하는 요청에는 `inspect --no-ui`를 사용합니다.
2. `scenario.status`가 `runnable`이고 이름이 요청과 일치하면 `python camera_test.py run galaxy.portrait-video --duration 5`처럼 실행합니다. 발견된 모드와 등록된 시나리오가 다르면 관찰 결과와 지원 범위를 보고합니다. 빌드 배포나 기능 설정을 요청받았다면 해당 개발 프로젝트의 설정 절차를 먼저 적용합니다.
3. 특정 기능을 확인하려면 `--check <name>`과 팀 검사 파일 경로를 사용합니다. `checks.registered_for_device`에서 현재 SoC에 적용되는 정의를 확인합니다. 검사 파일은 `--checks-file <path>`로 전달합니다. 결과가 `UNVERIFIED`이면 `feature.reason`을 보고합니다. HAL 검사 정의 형식은 저장소 루트의 `README.md`에 있습니다.
4. JSON의 `scenario_status`와 `feature.status`를 각각 전달하고 실행 ID와 로그 경로를 알려 줍니다. 실패 분석에는 관련 로그 구간을 읽습니다. `recording_may_be_active`가 `true`이면 재실행 전에 단말의 녹화 상태를 확인합니다.
