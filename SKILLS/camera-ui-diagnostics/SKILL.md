---
name: camera-ui-diagnostics
description: Investigate the UI and state transitions of a connected Android camera app when adding a scenario, handling a new app build, or diagnosing a missing control or broken selector. Use Android UI hierarchy, app lifecycle signals, and scoped logs to establish how to automate the flow.
---

# 카메라 UI 자동화 진단

등록된 동작을 재현할 때는 `camera-test` 스킬을 사용합니다. 새 모드, 다른 카메라 앱, UI가 달라진 앱 빌드를 조사할 때 아래 절차를 사용합니다.

1. 저장소 루트에서 `python camera_test.py inspect --device <serial>`을 실행합니다. 기기 모델, SoC, 앱 패키지와 버전, 요청받은 모드를 기록합니다. 갤럭시 기본 앱 외의 앱은 해당 앱의 실제 패키지와 실행 Activity를 먼저 확인합니다.
2. 모드 스트립, 더보기, 모드 진입 후, 조작 버튼이 있는 화면에서 필요한 UI 요소만 조사합니다. Android CLI가 이미 있으면 `android layout --device=<serial> --full`을 사용합니다. ADB만 쓰는 환경에서는 아래처럼 `uiautomator dump`를 사용합니다.

   ```powershell
   adb -s SERIAL shell 'uiautomator dump /data/local/tmp/camera-ui-diagnostics.xml >/dev/null && cat /data/local/tmp/camera-ui-diagnostics.xml; rm -f /data/local/tmp/camera-ui-diagnostics.xml'
   ```

3. 앱 패키지와 리소스 ID를 우선 사용합니다. 중복되면 접근성 설명, 선택 상태, 부모 요소를 함께 확인합니다. 조작 위치는 현재 노드의 좌표에서 계산합니다. 계층으로 해석하기 어려우면 스크린샷에서 해당 컨트롤을 확인하고, 확인한 화면과 앱 버전을 기록합니다.
4. 각 동작 직후에 확인할 상태를 정합니다. 녹화 타이머 때문에 UI 덤프가 대기하는 경우에는 해당 앱 프로세스의 녹화 상태 로그를 확인합니다. 종료는 상태 전이와 이번 실행에서 생성된 파일을 확인합니다. 기능별 Camera HAL 판정에는 해당 SoC와 빌드에서 직접 확인한 태그와 메시지를 사용합니다.
5. 기존 시나리오가 새 앱 빌드에서 실패했다면 코드보다 앱 프로필을 먼저 고칩니다. `run ... --allow-unverified-version` 결과의 `diagnosis.ui.absent[*].similar`와 `artifacts.failure_ui`로 바뀐 리소스 ID를 확인하고, `diagnosis.record_state_log`에서 `lines > 0`이고 `matched == 0`이면 녹화 상태 로그 패턴을 확인합니다. `profile show <scenario> --output <path>`로 내보낸 파일을 고치고 `profile validate <path>`로 검사한 뒤 `run --profile-file <path>`로 재현합니다. 후보 ID는 이름이 비슷할 뿐이므로, 덤프에서 실제 컨트롤인지 확인한 뒤에 반영합니다.
6. 화면 흐름 자체가 바뀌었거나 새 모드를 추가하는 경우에는, 관찰한 식별자와 상태 조건을 시나리오 코드와 내장 프로필에 반영한 뒤, 평소 시작 화면과 이미 대상 모드가 열린 상태에서 재현합니다. 결과에는 검증한 앱 버전과 SoC, 모드 선택·시작·종료·저장 신호를 기록합니다.
