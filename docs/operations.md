# 운영 가이드

새 카메라 앱 빌드를 검증하고, 깨진 자동화를 고치고, 전용 랩 단말로 자동 검증을 운영하는 담당자를 위한 문서입니다. 처음 쓰는 방법은 [README](../README.md)에 있습니다.

## 새 앱 빌드 검증

새 빌드가 설치되면 `verify-build`로 반복 검증합니다. 각 반복은 `run`에 영상 판정, 기준 화면 비교, 영상 정리, 사용 중인 단말 보호를 켠 것과 같습니다.

```powershell
python camera-test.pyz verify-build galaxy.portrait-video --repeat 5 --golden golden/galaxy.portrait-video/17.0.00.43
```

- 5회 기준 4~6분 걸립니다. 결과는 `runs/verify/<batch-id>/`에 저장되며, `report.md`에 결정과 다음 명령이 정리됩니다.
- 출력 JSON의 `decision`과 `next_action`을 따릅니다.
- 이전 빌드의 `report.json`을 `--baseline`으로 주면, 지연 시간 중앙값이 25%와 100ms를 모두 넘게 늘었을 때 `METRIC_REGRESSION` 경고가 붙습니다.

| `decision` | 뜻 | 종료 코드 |
|---|---|---|
| `promote` | 새 버전이 전부 통과했습니다. [제안을 승인](#제안-승인)합니다. | `0` |
| `baseline_ok` | 이미 검증된 버전이 통과했습니다. `report.json`을 다음 빌드의 기준으로 보관합니다. | `0` |
| `needs_profile_fix` | 자동화가 앱과 맞지 않거나, 기준 화면 대비 `breaking` 변화가 있습니다. [프로필을 고칩니다](#프로필-고치기). | `1` |
| `product_failure` | 앱이 실제로 잘못 동작합니다. 결함으로 보고합니다. | `1` |
| `flaky` | 원인 분류가 안 되는 혼합 결과입니다. 실행별 `result.json`을 봅니다. | `1` |
| `environment_blocked` | 단말 상태 때문에 멈췄습니다. `doctor`로 확인한 뒤 다시 실행합니다. | `3` |

### 제안 승인

`promote`이면 배치 폴더의 `proposal/`에 새 버전을 추가한 프로필(`profile.json`)과 기준 화면(`golden/`)이 생깁니다. 도구는 코드를 직접 바꾸지 않으며, 사람이 확인하고 반영합니다.

1. `report.md`에서 통과율, 지연 시간, 영상 요약이 예상과 맞는지 확인합니다.
2. `camera_runner/scenarios/galaxy_portrait_video.py`의 `PROFILE`에서 `verified_versions`에 새 버전을 추가합니다. 제안 프로필에서 바뀐 값이 있으면 함께 옮깁니다.
3. `proposal/golden/`을 `golden/galaxy.portrait-video/<새 버전>/`으로 복사합니다.
4. `python build.py camera-test.pyz`로 다시 빌드하고, 테스트를 통과시킨 뒤 PR로 올립니다.

기준 화면 스냅샷에는 카메라 앱 노드의 리소스 ID, 문구, 클래스만 있고 좌표나 다른 앱 정보는 없으므로, `golden/`은 저장소에 커밋해 공유합니다. 반대로 배치 폴더와 `runs/`에는 화면 캡처와 로그가 있으므로 커밋하지 않습니다.

## 프로필 고치기

자동화가 의존하는 리소스 ID, UI 문구, 로그 패턴은 코드가 아니라 앱 프로필에 있습니다. `automation` 실패나 `needs_profile_fix`는 대부분 프로필만 고쳐서 해결합니다.

1. 실패한 실행의 `result.json`에서 단서를 봅니다.
   - `diagnosis.phase`: 실패한 단계입니다.
   - `diagnosis.ui.absent[*].similar`: 찾지 못한 선택자와 이름이 비슷한 실제 리소스 ID 후보입니다.
   - `diagnosis.record_state_log`: `lines > 0`이고 `matched == 0`이면 녹화 상태 로그의 문구가 바뀐 것입니다.
   - `drift.checkpoints.*.profile_selectors_missing`: 기준 화면에 있었는데 사라진 선택자입니다.
2. 내장 프로필을 파일로 내보냅니다. PowerShell의 `>`는 파일을 UTF-16으로 저장해 한글이 깨질 수 있으므로 `--output`을 씁니다.

   ```powershell
   python camera-test.pyz profile show galaxy.portrait-video --output galaxy-camera.json
   ```

3. 바뀐 값을 고칩니다. 후보 ID는 이름이 비슷하다는 뜻일 뿐이므로, 실패 시점의 화면 구조(`artifacts.failure_ui`)에서 실제 컨트롤인지 확인한 뒤 반영합니다.

   | 필드 | 내용 |
   |---|---|
   | `verified_versions` | 검증된 앱 버전 목록입니다. |
   | `selectors` | 단계별로 찾는 리소스 ID입니다. 키 이름은 바꿀 수 없습니다. |
   | `labels` | 모드 이름, 더보기, 녹화 시작 버튼의 문구(한국어, 영어)입니다. |
   | `log.record_state.pattern` | 녹화 상태 전환 로그 정규식입니다. 이전·다음 상태 두 그룹을 캡처해야 합니다. |
   | `volatile_ids` | 기준 화면 비교에서 제외할 일시적 요소입니다. |

4. 형식을 검사하고 다시 검증합니다. 키 오타나 알 수 없는 필드는 검사 단계에서 거부됩니다.

   ```powershell
   python camera-test.pyz profile validate galaxy-camera.json
   python camera-test.pyz verify-build galaxy.portrait-video --profile-file galaxy-camera.json --repeat 3
   ```

5. `promote`가 나오면 [제안 승인](#제안-승인)으로 반영합니다.

화면 흐름 자체가 바뀌었다면 프로필만으로는 대응할 수 없습니다. [camera-ui-diagnostics 스킬](../SKILLS/camera-ui-diagnostics/SKILL.md) 절차로 시나리오 코드를 고칩니다.

## 기준 화면

실행이 통과해도 UI가 바뀌었는지는 알 수 없습니다. 검증된 빌드의 화면을 기준으로 저장해 두면, 아직 깨지지 않은 변화도 `drift`로 미리 보입니다. `drift`는 판정과 종료 코드를 바꾸지 않으며, 프로필 선택자나 프로필 문구가 바뀐 `breaking` 변화만 `UI_DRIFT_BREAKING` 경고를 붙입니다.

갤럭시 카메라는 마지막에 쓴 모드를 기억하므로, 시작 상태별로 저장되는 화면이 다릅니다. 반복 실행은 대부분 인물 동영상 모드에서 시작하므로 두 상태를 모두 넣어야 시작 화면까지 비교됩니다.

1. 카메라를 사진 모드로 바꿔 둔 뒤 실행하고, 기준 화면을 만듭니다(`initial`, `more_opened`, `mode_ready`).

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --capture-screens --cleanup-video
   python camera-test.pyz screens promote --run runs/<run-id> --output golden/galaxy.portrait-video/17.0.00.43
   ```

2. 인물 동영상 모드에 남은 상태에서 한 번 더 실행하고 합칩니다(`initial_in_mode`).

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --capture-screens --cleanup-video
   python camera-test.pyz screens promote --run runs/<run-id> --output golden/galaxy.portrait-video/17.0.00.43 --merge
   ```

앱이 그대로인데 같은 `cosmetic` 차이가 반복되면, 덤프 시점이나 단말 자세에 따라 바뀌는 요소입니다. 그 리소스 ID를 프로필의 `volatile_ids`에 추가합니다. 내장 프로필에는 진입 직후의 "준비되었어요" 안내(`guide_text`)와 수평계 요소가 들어 있습니다.

## 랩 단말 자동 운영

전용 랩 단말을 PC에 연결해 두고 매시간 `verify-build --if-changed`를 실행하면, 앱 버전이 바뀐 때만 단말을 조작하고 나머지 시간에는 아무것도 하지 않습니다.

> **주의** 자동 실행은 단말 화면을 누르고 녹화합니다. 사람이 쓰는 단말이 아니라 **전용 랩 단말**에만 등록합니다.

### 1. 랩 단말 설정

| 설정 | 값 |
|---|---|
| 화면 잠금 | 없음 |
| 개발자 옵션 > 충전 중 화면 켜짐 유지 | 켬 (충전기 상시 연결) |
| 연결 | USB 권장. 무선 디버깅은 재부팅하면 포트가 바뀝니다. |
| 앞 화면 | 홈 화면 또는 카메라. 다른 앱이 떠 있으면 실행을 건너뜁니다. |

설정 후 `python camera-test.pyz doctor`의 모든 항목이 `[OK]`인지 확인합니다.

### 2. 기준 준비

1. 검증된 빌드에서 [기준 화면](#기준-화면)을 만들거나, 저장소의 `golden/galaxy.portrait-video/<버전>/`을 씁니다.
2. 같은 빌드에서 기준 보고서를 만듭니다. 보고서와 상태 파일은 저장소 밖 고정 폴더(예: `D:\camera-lab\`)에 둡니다.

   ```powershell
   python camera-test.pyz verify-build galaxy.portrait-video --repeat 5 --golden golden/galaxy.portrait-video/17.0.00.43 --state-file D:\camera-lab\verify-state.json --output-dir D:\camera-lab\verify
   ```

   결정이 `baseline_ok`이면 그 `report.json`을 `D:\camera-lab\baseline\report.json`으로 복사합니다. 참고로 `SM-S936N`, `17.0.00.43`, 무선 ADB에서 측정한 중앙값은 녹화 시작 지연 약 290ms, 종료 지연 약 1,440ms, 저장 완료 약 2,270ms였습니다.

### 3. 매시간 작업 등록

`schtasks /TR`은 명령 길이가 261자로 제한되므로, 명령을 배치 파일에 넣습니다.

1. `D:\camera-lab\verify-build.cmd`를 만듭니다. 저장소 경로는 `C:\camera-test`로 가정했습니다.

   ```bat
   @echo off
   rem ADB가 PATH에 없으면 다음 줄의 rem을 지우고 경로를 고칩니다.
   rem set CAMERA_TEST_ADB=C:\Android\Sdk\platform-tools\adb.exe
   cd /d C:\camera-test
   python camera-test.pyz verify-build galaxy.portrait-video --if-changed ^
     --state-file D:\camera-lab\verify-state.json ^
     --output-dir D:\camera-lab\verify ^
     --golden golden\galaxy.portrait-video\17.0.00.43 ^
     --baseline D:\camera-lab\baseline\report.json ^
     >> D:\camera-lab\verify-build.log 2>&1
   ```

2. 배치 파일을 한 번 직접 실행해, `verify-build.log`에 결과 JSON 한 줄이 남는지 확인합니다.

   ```powershell
   D:\camera-lab\verify-build.cmd
   ```

3. 작업을 등록합니다. 매시간 5분에 실행되며, 등록한 사용자가 로그인해 있을 때만 동작합니다.

   ```powershell
   schtasks /Create /TN "CameraTest\VerifyBuild" /SC HOURLY /ST 00:05 /F /TR "D:\camera-lab\verify-build.cmd"
   ```

등록 확인은 `schtasks /Query /TN "CameraTest\VerifyBuild"`, 삭제는 `schtasks /Delete /TN "CameraTest\VerifyBuild" /F`로 합니다.

### 4. 운영 중 확인

- 최근 결과는 `D:\camera-lab\verify\`의 가장 최근 폴더에 있는 `report.md`입니다. 상태 파일에는 단말별 마지막 버전과 결정이 있습니다.
- `environment_blocked`로 끝난 빌드는 다음 시간에 다시 시도합니다. 그 밖의 결정(실패 포함)은 같은 빌드에서 반복하지 않습니다.
- 프로필을 고쳤거나 같은 빌드를 다시 확인하려면 `--if-changed` 없이 실행합니다.
- 새 빌드를 승인했다면 배치 파일의 `--golden`과 `--baseline`을 새 버전 것으로 바꿉니다.
