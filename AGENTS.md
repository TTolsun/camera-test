# 저장소 작업 지침

- 사내 PC의 같은 저장소 폴더에서 실행합니다. 전체 모드 검사·등록 요청은 `python camera-test.pyz suite --register-device --json`, 이 단말에서 무엇을 검증했는지 묻는 요청은 `python camera-test.pyz capabilities --json`을 사용합니다.
- 특정 모드 실행은 `python camera-test.pyz mode "<요청한 모드>" --register-device --json`을 사용합니다. “Pro Video 동작시켜줘”는 후면 프로 동영상 1회, “전면 야간 촬영해줘”는 `mode "야간" --facing front --register-device --json`입니다. 모드 이름은 `mode --list`에서 확인합니다.

- “열어줘”에는 `--open-only`를 사용합니다. 연속 동작은 같은 단말에서 순서대로 실행하고 한 단계가 실패하면 멈춥니다. 전·후면 각각의 지원 여부를 검사해 달라고 하면 두 방향을 독립적으로 검사합니다. 한쪽에서 모드나 방향을 확인하지 못해도 다른 쪽을 검사하며, 환경·등록 오류나 녹화 중단 실패 시에는 멈춥니다.
- 카메라 모드 확인, 등록된 시나리오 실행, Camera HAL 로그 수집에는 [camera-test](SKILLS/camera-test/SKILL.md)를 사용합니다. 실행은 저장소 루트에서 시작합니다.
- 새 앱·모드 자동화, 앱 버전 변경, 선택자 진단에는 [camera-ui-diagnostics](SKILLS/camera-ui-diagnostics/SKILL.md)를 사용합니다.
- Android CLI, SDK 및 기기 설정을 조사할 때는 [android-cli](SKILLS/android-cli/SKILL.md)를 참고합니다. 위 두 스킬의 기본 실행에는 Python과 ADB가 필요합니다.
