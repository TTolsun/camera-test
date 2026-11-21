# 랩 단말 자동 운영

전용 랩 단말을 PC에 연결해 두고, 새 카메라 앱 빌드가 설치될 때마다 자동으로 검증하는 방법입니다. 매시간 `verify-build --if-changed`를 실행하면, 앱 버전이 바뀐 때만 단말을 조작하고 나머지 시간에는 아무것도 하지 않습니다.

> **주의** 자동 실행은 단말 화면을 누르고 녹화합니다. 사람이 쓰는 단말이 아니라 **전용 랩 단말**에만 등록합니다. 카메라나 홈 화면이 아닌 앱이 떠 있으면 실행하지 않지만(`PRECHECK_DEVICE_BUSY`), 이 검사가 모든 상황을 막지는 못합니다.

## 운영 구조

```
매시간 작업 스케줄러
  → verify-build --if-changed --state-file ...
     ├─ 앱 버전이 상태 파일과 같음   → unchanged (단말을 건드리지 않음, 종료 코드 0)
     └─ 앱 버전이 바뀜(새 빌드)     → 5회 반복 검증 → report.md / report.json
                                       → 상태 파일에 결과 기록
                                       → 담당자가 decision에 따라 조치
```

`environment_blocked`로 끝난 빌드는 아무것도 확인하지 못했으므로, 다음 시간에 다시 시도합니다. 그 밖의 결정(실패 포함)은 같은 빌드에서 반복하지 않습니다.

## 1. 랩 단말 설정

| 설정 | 값 | 이유 |
|---|---|---|
| 화면 잠금 | 없음 | 잠금 화면이면 `PRECHECK_LOCKED`로 중단합니다. |
| 개발자 옵션 > 충전 중 화면 켜짐 유지 | 켬 | 화면이 꺼지거나 절전(`Dozing`)이면 `PRECHECK_SCREEN_OFF`로 중단합니다. |
| 충전 | 상시 연결 | 화면 유지와 장시간 운영을 위해 필요합니다. |
| 연결 | USB 권장 | 무선 디버깅은 재부팅하면 포트가 바뀌어 다시 `adb connect`를 해야 합니다. |
| 카메라 앱 | 홈 화면 또는 카메라만 띄워 둠 | 다른 앱이 떠 있으면 `PRECHECK_DEVICE_BUSY`로 건너뜁니다. |
| 저장 공간 | 500MB 이상 | 반복 실행은 `--cleanup-video`로 이번 실행이 만든 영상만 지웁니다. |

## 2. 기준 화면과 기준 보고서 준비

1. 검증된 빌드에서 기준 화면을 만듭니다. 절차는 [기준 화면 만들기](new-app-build.md#기준-화면-만들기)에 있습니다. 이미 저장소에 `golden/galaxy.portrait-video/<버전>/`이 있으면 그것을 씁니다.
2. 같은 빌드에서 기준 보고서를 만듭니다. 결정이 `baseline_ok`이면 이 `report.json`을 다음 빌드의 비교 기준으로 씁니다.

   ```powershell
   python camera-test.pyz verify-build galaxy.portrait-video --repeat 5 --golden golden/galaxy.portrait-video/17.0.00.43 --state-file D:\camera-lab\verify-state.json --output-dir D:\camera-lab\verify
   ```

보고서와 상태 파일은 저장소 밖의 고정 폴더(예: `D:\camera-lab\`)에 둡니다. 보고서 폴더에는 단말 화면 캡처와 로그가 들어 있으므로 저장소에 커밋하지 않습니다.

## 3. 매시간 작업 등록 (Windows 작업 스케줄러)

`schtasks /TR`은 명령 길이가 261자로 제한되므로, 실행할 명령을 배치 파일에 넣고 작업은 그 파일을 실행하게 합니다.

1. `D:\camera-lab\verify-build.cmd`를 만듭니다. 경로는 실제 값으로 바꿉니다. 저장소 경로는 `C:\camera-test`로 가정했습니다.

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

2. 단말이 연결된 PC에서 작업을 한 번 등록합니다. 매시간 5분에 실행됩니다.

   ```powershell
   schtasks /Create /TN "CameraTest\VerifyBuild" /SC HOURLY /ST 00:05 /F /TR "D:\camera-lab\verify-build.cmd"
   ```

3. 배치 파일을 직접 한 번 실행해 `verify-build.log`에 결과 JSON 한 줄이 남는지 확인합니다.

   ```powershell
   D:\camera-lab\verify-build.cmd
   ```

등록 확인과 삭제는 다음 명령으로 합니다.

```powershell
schtasks /Query /TN "CameraTest\VerifyBuild"
```

```powershell
schtasks /Delete /TN "CameraTest\VerifyBuild" /F
```

이렇게 등록한 작업은 등록한 사용자가 로그인해 있을 때만 실행됩니다. 실행 기록은 `verify-build.log`에 한 줄씩 쌓입니다.

## 4. 결과 확인

- 가장 최근 배치는 `D:\camera-lab\verify\` 아래에서 가장 최근 폴더입니다. `report.md`를 먼저 엽니다.
- 상태 파일(`verify-state.json`)에는 단말별 마지막 앱 버전, 결정, 보고서 경로가 있습니다.
- 결정별 조치는 [새 앱 빌드 대응](new-app-build.md#2-결정별-조치)을 따릅니다.
- 새 빌드를 승인했다면 기준 화면 경로(`--golden`)와 기준 보고서(`--baseline`)를 새 버전 것으로 바꿉니다.

## 5. 같은 빌드를 다시 검증하려면

`--if-changed`는 같은 버전을 건너뜁니다. 프로필을 고쳤거나 같은 빌드를 다시 확인하려면 `--if-changed` 없이 실행합니다.

```powershell
python camera-test.pyz verify-build galaxy.portrait-video --repeat 5 --profile-file galaxy-camera.json --state-file D:\camera-lab\verify-state.json --output-dir D:\camera-lab\verify
```
