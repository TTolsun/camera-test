# 새 앱 빌드 대응

새 카메라 앱 빌드가 설치되면 `verify-build`로 반복 검증하고, 결과로 나온 결정(`decision`)에 따라 조치합니다. 자동화가 의존하는 리소스 ID, UI 문구, 로그 패턴은 코드가 아니라 **앱 프로필(JSON)**에 있으므로, 앱이 바뀌어도 대부분 프로필만 고쳐서 대응합니다.

## 한눈에 보는 흐름

```
새 빌드 설치
  → verify-build (기본 5회 반복, 영상 판정·기준 화면 비교 포함)
  → decision
     ├─ promote            : 통과. 제안된 프로필·기준 화면을 승인합니다.
     ├─ baseline_ok        : 이미 검증된 버전이고 통과했습니다. 할 일이 없습니다.
     ├─ needs_profile_fix  : 자동화가 앱과 맞지 않습니다. 프로필을 고칩니다.
     ├─ product_failure    : 앱이 실제로 잘못 동작합니다. 결함으로 보고합니다.
     ├─ flaky              : 원인 분류가 안 되는 혼합 결과입니다. 실행별 결과를 봅니다.
     └─ environment_blocked: 단말 상태 문제로 멈췄습니다. 단말을 고치고 다시 실행합니다.
```

## 1. verify-build 실행

```powershell
python camera-test.pyz verify-build galaxy.portrait-video --repeat 5 --golden golden/galaxy.portrait-video/17.0.00.43
```

- 5회 기준으로 4~6분 정도 걸립니다.
- 각 반복은 `--allow-unverified-version --verify-media --cleanup-video --require-idle --capture-screens`를 적용한 `run`과 같습니다.
- 단말 문제(`environment`)가 한 번이라도 나오거나, 자동화 실패(`automation`)가 2회 연속 나오면 남은 반복을 건너뜁니다.
- 결과는 `runs/verify/<batch-id>/`에 저장됩니다. 먼저 `report.md`를 엽니다. 결정, 핵심 숫자, 다음에 실행할 명령이 정리되어 있습니다.
- 이전 빌드의 `report.json`이 있으면 `--baseline <경로>`를 추가합니다. 지연 시간 중앙값이 25%와 100ms를 모두 넘게 늘면 `METRIC_REGRESSION` 경고가 붙습니다.

## 2. 결정별 조치

| `decision` | 종료 코드 | 조치 |
|---|---|---|
| `promote` | `0` | [제안 승인](#제안-승인promote)을 진행합니다. |
| `baseline_ok` | `0` | 할 일이 없습니다. 이 `report.json`을 다음 빌드의 `--baseline`으로 보관합니다. |
| `needs_profile_fix` | `1` | [프로필 고치기](#실행이-깨졌을-때-프로필-고치기)를 진행합니다. 모두 통과했더라도 기준 화면 대비 `breaking` 변화가 있으면 이 결정이 나옵니다. |
| `product_failure` | `1` | 실패한 실행의 `logcat.txt`, `failure.png`, 영상을 첨부해 앱 결함으로 보고합니다. `iterations.intermittent`가 `true`이면 간헐적인 결함입니다. |
| `flaky` | `1` | 분류표에 없는 오류 코드가 섞였습니다. `report.json`의 `error_codes`와 실행별 `result.json`을 확인합니다. |
| `environment_blocked` | `3` | 단말을 켜고, 잠금을 풀고, 다른 앱을 닫은 뒤 다시 실행합니다. |

## 제안 승인(promote)

`promote`이면 배치 폴더의 `proposal/`에 새 버전을 추가한 프로필(`profile.json`)과 기준 화면(`golden/`)이 생깁니다. 도구는 코드를 직접 바꾸지 않으며, 사람이 확인한 뒤 반영합니다.

1. `report.md`에서 통과율, 지연 시간, 영상 요약(코덱, 해상도, fps, 길이)이 예상과 맞는지 확인합니다.
2. 제안 프로필을 검사하고 한 번 더 실행합니다.

   ```powershell
   python camera-test.pyz profile validate runs/verify/<batch-id>/proposal/profile.json
   python camera-test.pyz run galaxy.portrait-video --profile-file runs/verify/<batch-id>/proposal/profile.json --verify-media --cleanup-video
   ```

3. 팀 전체에 반영합니다.
   - `camera_runner/scenarios/galaxy_portrait_video.py`의 `PROFILE`에서 `verified_versions`에 새 버전을 추가합니다. 제안 프로필에서 다른 값이 바뀌었다면 그 값도 옮깁니다.
   - `proposal/golden/`을 `golden/galaxy.portrait-video/<새 버전>/`으로 복사합니다.
   - `python build.py camera-test.pyz`로 다시 빌드하고, 테스트(`python -m unittest discover -s tests`)를 통과시킨 뒤 PR로 올립니다.

> 기준 화면 스냅샷에는 카메라 앱 노드의 리소스 ID, 문구, 클래스만 들어 있고 좌표나 다른 앱 정보는 없습니다. 그래서 `golden/` 폴더는 저장소에 커밋해 팀이 공유하는 것을 권장합니다. 반면 `runs/`에는 화면 캡처와 로그가 있으므로 커밋하지 않습니다.

## 실행이 깨졌을 때 프로필 고치기

`needs_profile_fix`이거나 `run`이 `automation` 실패로 끝났을 때의 절차입니다.

1. **실패한 단계와 후보를 확인합니다.** `report.md`가 가리키는 실행의 `result.json`에서 `diagnosis`를 봅니다.

   | 필드 | 읽는 법 |
   |---|---|
   | `diagnosis.phase` | 실패한 단계입니다: `launch`, `mode_select`, `mode_ready`, `recording`. |
   | `diagnosis.ui.absent` | 그 단계가 의존하는데 화면에서 찾지 못한 선택자입니다. `similar`에는 이름이 비슷한 실제 리소스 ID 후보가 있습니다. |
   | `diagnosis.record_state_log` | `lines > 0`이고 `matched == 0`이면 녹화 상태 로그의 문구가 바뀐 것입니다. |
   | `artifacts.failure_ui` | 실패 시점의 화면 구조(XML)입니다. 후보가 실제 컨트롤인지 여기서 확인합니다. |
   | `drift.checkpoints.*.profile_selectors_missing` | 기준 화면에는 있었는데 사라진 프로필 선택자입니다. |

2. **내장 프로필을 파일로 내보냅니다.** PowerShell의 `>`는 쓰지 않습니다(UTF-16으로 저장되어 한글이 깨질 수 있습니다).

   ```powershell
   python camera-test.pyz profile show galaxy.portrait-video --output galaxy-camera.json
   ```

3. **파일을 고칩니다.** 주요 필드는 다음과 같습니다.

   | 필드 | 내용 |
   |---|---|
   | `verified_versions` | 검증된 앱 버전 목록입니다. 새 버전을 추가합니다. |
   | `selectors` | 단계별로 찾는 리소스 ID입니다. 바뀐 ID로 고칩니다. 키 이름은 바꿀 수 없습니다. |
   | `labels` | 모드 이름, 더보기, 녹화 시작 버튼의 문구입니다. 한국어와 영어를 함께 둡니다. |
   | `log.record_state.pattern` | 녹화 상태 전환 로그 정규식입니다. 이전 상태와 다음 상태 두 그룹을 캡처해야 합니다. |
   | `volatile_ids` | 기준 화면 비교에서 제외할 일시적 요소입니다. [아래](#일시적-요소volatile_ids)를 참고합니다. |

   후보 ID는 이름이 비슷하다는 뜻일 뿐입니다. 반영하기 전에 `failure-ui.xml`에서 실제로 그 컨트롤인지 확인합니다.

4. **ADB 없이 검사하고, 실기기에서 다시 검증합니다.** 알 수 없는 키나 오타가 있으면 검사 단계에서 거부됩니다.

   ```powershell
   python camera-test.pyz profile validate galaxy-camera.json
   python camera-test.pyz verify-build galaxy.portrait-video --profile-file galaxy-camera.json --repeat 3
   ```

5. 결정이 `promote`가 되면 [제안 승인](#제안-승인promote)으로 팀 전체에 반영합니다.

화면 흐름 자체가 바뀌었다면(예: 모드 선택 방식이 달라짐) 프로필만으로는 대응할 수 없습니다. 이때는 [camera-ui-diagnostics 스킬](../SKILLS/camera-ui-diagnostics/SKILL.md) 절차로 시나리오 코드를 고칩니다.

## 기준 화면(golden)으로 UI 변화 미리 보기

실행이 통과해도 UI가 바뀌었는지는 알 수 없습니다. 검증된 빌드의 화면을 기준으로 저장해 두면, 새 빌드에서 아직 깨지지 않은 변화도 미리 볼 수 있습니다.

### 기준 화면 만들기

갤럭시 카메라는 마지막에 쓴 모드를 기억하므로, 시작 상태에 따라 저장되는 화면(체크포인트)이 다릅니다.

| 시작 상태 | 저장되는 체크포인트 |
|---|---|
| 다른 모드(예: 사진)에서 시작 | `initial`, `more_opened`, `mode_ready` |
| 이미 인물 동영상 모드에서 시작 | `initial_in_mode`, `mode_ready` |

반복 실행은 대부분 인물 동영상 모드에서 시작하므로, 두 시작 상태를 모두 기준 화면에 넣어야 시작 화면까지 비교됩니다.

1. 카메라를 사진 모드로 바꿔 둔 뒤 실행하고, 기준 화면으로 승격합니다.

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --capture-screens --cleanup-video
   python camera-test.pyz screens promote --run runs/<run-id> --output golden/galaxy.portrait-video/17.0.00.43
   ```

2. 카메라가 인물 동영상 모드에 남아 있는 상태에서 한 번 더 실행하고, `--merge`로 합칩니다.

   ```powershell
   python camera-test.pyz run galaxy.portrait-video --capture-screens --cleanup-video
   python camera-test.pyz screens promote --run runs/<run-id> --output golden/galaxy.portrait-video/17.0.00.43 --merge
   ```

`golden.json`의 `checkpoints`에 체크포인트 4개가 모두 있으면 완성입니다.

### 비교 결과 읽기

`run --golden` 또는 `verify-build --golden`의 결과에 `drift`가 붙습니다. drift는 판정(`scenario_status`)과 종료 코드를 바꾸지 않습니다.

| `severity` | 뜻 | 경고 |
|---|---|---|
| `breaking` | 프로필 선택자가 사라졌거나, 프로필 문구를 가진 컨트롤의 문구가 바뀌었습니다. 곧 실행이 깨질 가능성이 높습니다. | `UI_DRIFT_BREAKING` |
| `cosmetic` | 그 밖의 ID 추가·삭제나 문구 변경입니다. 보고서에만 남습니다. | 없음 |
| `none` | 차이가 없습니다. 이번 실행이 지나가지 않은 체크포인트는 `not_reached: true`로 표시합니다. | 없음 |

저장된 실행은 ADB 없이 다시 비교할 수 있습니다.

```powershell
python camera-test.pyz screens compare --golden golden/galaxy.portrait-video/17.0.00.43 --run runs/<run-id>
```

### 일시적 요소(volatile_ids)

앱이 그대로인데도 `cosmetic` 차이가 계속 나온다면, 덤프 시점이나 단말 자세에 따라 보였다 안 보였다 하는 요소일 가능성이 높습니다. 그런 리소스 ID는 프로필의 `volatile_ids`에 추가해 비교에서 제외합니다. 내장 프로필에는 실기기에서 관찰한 다음 요소가 들어 있습니다.

| 리소스 ID | 이유 |
|---|---|
| `guide_text` | 인물 동영상 진입 직후 잠깐 뜨는 "준비되었어요" 안내 문구입니다. |
| `level_meter_*`, `guide_bar`, `inclinometer`, `level_bg`, `vertical_guide_bg` | 수평계입니다. 단말이 기울어진 정도에 따라 표시되는 노드가 바뀝니다. |

프로필 선택자는 `volatile_ids`에 넣을 수 없습니다.
