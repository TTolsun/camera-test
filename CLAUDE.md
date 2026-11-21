# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

작업별 스킬 선택은 [AGENTS.md](AGENTS.md)를 따른다. 사용자용 설명은 [README.md](README.md)(시작 안내)와 `docs/`(설치, 일상 검증, 새 앱 빌드 대응, 랩 자동 운영, 참조)에 있다. CLI 옵션이나 결과 필드를 바꾸면 `docs/reference.md`와 해당 가이드를 함께 고친다.

## 명령

모든 명령은 저장소 루트에서 실행한다. Python 3.10 이상, 표준 라이브러리만 사용한다(외부 의존성 없음).

```powershell
python -m unittest discover -s tests                     # 전체 테스트 (ADB·실기기 불필요)
python -m unittest tests.test_camera_hal.HalCommandTests.test_compare_summarizes_tags_and_messages   # 단일 테스트
python camera_test.py inspect --pretty                   # 소스에서 실행 (실기기 필요)
python build.py camera-test.pyz                          # 배포용 단일 실행 파일 갱신
```

- `camera-test.pyz`는 Git에 커밋되는 배포물이다. `camera_runner/` 또는 `camera_test.py`를 수정하면 `python build.py camera-test.pyz`로 다시 빌드해 함께 커밋한다. 인자 없이 실행하면 `dist/camera-test.pyz`에 생성된다.
- 린터·포매터 설정은 없다.

## 아키텍처

CLI(`camera_runner/cli.py`)가 `list`, `inspect`, `run`, `verify-build`, `profile {show,validate}`, `screens {promote,compare}`, `media inspect`, `hal {begin,collect,compare}` 하위 명령을 제공하고, 결과를 JSON으로 stdout에 출력한다. 오류는 `RunError(code, message)`로 올라와 JSON의 오류 코드가 된다.

- **ADB 전송 계층** (`adb.py`): 모든 기기 통신은 모듈 함수 `adb.adb(*args)` 하나를 거친다. `Device`는 그 위에서 shell, UI 덤프, logcat, pull을 제공한다. `device_lock`은 임시 디렉터리 파일 잠금으로 같은 기기에 대한 동시 실행을 막는다(`DEVICE_BUSY`). 사람이 단말을 쓰는 중인지는 `preflight`의 `--require-idle` 점검(`PRECHECK_DEVICE_BUSY`: 최상단 앱이 카메라도 HOME 런처도 아님)이 따로 판단하며 `verify-build`에서는 항상 켜진다. `select_device`는 무선 디버깅이 한 기기를 두 번 나열해도 `ro.serialno`가 같으면 `ip:port` 쪽을 고른다(기기가 둘 이상일 때만 `getprop`을 호출한다).
- **시나리오 레지스트리** (`scenarios/__init__.py`): `SCENARIOS` dict 하나를 `list`, `inspect`, `run`이 공유한다. 시나리오는 `scan(device, profile)`(모드 탐색)과 `execute(device, profile, seconds, result)`(녹화 후 녹화 시작 시각 반환)를 제공하고, 선택적으로 `phases`(단계 이름 → 그 단계가 의존하는 프로필 선택자 키)를 선언한다. `execute`는 `device.phase`를 갱신하고, UI를 덤프한 직후 `device.checkpoint(name)`을 호출해 화면 스냅샷을 남긴다(녹화 중에는 덤프하지 않는다).
- **앱 프로필** (`profiles.py`): 시나리오가 의존하는 리소스 ID, UI 문구, 녹화 상태 로그 정규식, `verified_versions`를 데이터로 둔다. 내장 프로필은 시나리오 모듈의 `PROFILE` dict이고, `--profile-file`이 같은 키 집합을 가진 JSON으로 대체한다(키 불일치는 거부). `run`은 앱 버전이 `verified_versions`에 없으면 `--allow-unverified-version` 없이는 조작 전에 중단한다. 실패하면 마지막 UI 덤프(`Device.last_ui`)와 프로필을 비교한 `diagnosis`를 결과에 넣는다.
- **실행 전 점검** (`preflight.py`): `run`과 UI 탐색 `inspect`가 화면 조작 전에 화면 켜짐(`mWakefulness`), 잠금(`isKeyguardShowing`), `/sdcard` 여유 공간을 측정한다. 신호가 없으면 `unknown`으로 기록하고 실패시키지 않는다. 오류 코드는 `PRECHECK_*`다.
- **실패 분류** (`failures.py`): `CLASS_BY_CODE` 한 표가 오류 코드를 `environment`/`automation`/`product`로 나눠 `result["failure_class"]`가 된다. 새 오류 코드를 추가하면 이 표에도 넣는다.
- **화면 스냅샷** (`screens.py`): `run --capture-screens`가 `runs/<id>/screens/*.json`(앱 패키지 노드의 id·text·desc·class만)을 저장하고, `screens promote`가 PASS 실행을 골든 폴더(`golden.json` 포함)로 만들며, `screens compare`와 `run --golden`이 ADB 없이 `breaking`/`cosmetic`/`none` 변화를 판정한다. 변화는 `scenario_status`와 종료 코드에 영향을 주지 않고 `drift`와 `warnings`로만 나온다.
- **빌드 검증** (`verify.py`): `run`의 본문은 `cli.run_once(args) -> (result, exit_code)`로 분리되어 있고 `run`과 `verify-build`가 함께 쓴다. `verify-build`는 `run_once`를 주입받아 N회 반복하고(`environment` 실패 즉시 중단, `automation` 연속 2회 중단) `report.json`/`report.md`와 결정(`promote`, `baseline_ok`, `needs_profile_fix`, `product_failure`, `flaky`, `environment_blocked`)을 만든다. `promote`일 때만 `proposal/`(프로필 JSON + 골든)을 쓰고 코드와 내장 프로필은 절대 수정하지 않는다. 상태 파일은 임시 파일 + `os.replace`로 쓴다. 종료 코드 3은 환경 문제다(2는 `run`의 기능 판정 근거 없음).
- **기능 검사** (`checks.py`): `--checks-file` JSON(`schema_version: 1`)을 읽는다. `source: hal`은 `vendor`(`exynos`/`qualcomm`)가 필수이고, `source: app`은 `vendor`가 없어야 한다. 같은 이름에 vendor별 정의를 둘 수 있고, 감지된 SoC로 하나를 고른다. 판정은 녹화 시작 시각 이후 해당 태그 로그에 정규식이 매칭되는지로 한다.
- **HAL 시험** (`hal.py`): `begin`이 기기 시각을 기록한 trial JSON을 만들고, `collect`가 그 이후 logcat을 저장한다. `compare`는 저장된 `threadtime` 로그만 읽으므로 ADB 없이 동작한다.
- **logcat 형식** (`logcat.py`): 태그·시각·`threadtime` 줄 정규식을 수집, 비교, 검사가 공유한다. 연도 없는 시각 간 경과 시간(`elapsed_ms`)도 여기서 계산한다.
- **영상 검증** (`media.py`): ADB 없이 MP4 박스(`ftyp`, `moov/mvhd`, `trak/{tkhd,mdhd,hdlr,stsd,stts}`)를 읽어 `inspect(path)`가 길이·트랙·fps를 돌려주고, `verdict(info, requested_seconds)`가 `MEDIA_*` 실패와 경고를 정한다. 임계값은 파일 위쪽 상수다. `run --verify-media`가 이를 호출하고, `run`은 항상 `result["metrics"]`에 시작·종료 지연을 기록한다(갤럭시 시나리오의 `record`).

## 테스트 방식

테스트는(`tests/test_verify_build.py` 포함) 실기기 없이 `patch.object(adb, "adb", side_effect=MockCameraAdb())`로 ADB 호출 전체를 가짜로 대체하고, `camera_test.main()`을 `sys.argv` 패치로 호출해 stdout JSON을 검사한다. `MockCameraAdb`는 반복 실행도 흉내 낸다(녹화마다 새 MP4 이름, `video_plan`, 최상단 앱 `top`, 런처 해석). 처리하지 않는 ADB 명령에 `AssertionError`를 던지므로, 시나리오가 새 ADB 명령을 쓰면 mock에도 응답을 추가해야 한다. preflight(`dumpsys power`/`dumpsys window`/`df`)와 `rm`도 mock의 속성(`wakefulness`, `keyguard`, `free_kb`, `rm_fails`, `deleted`)으로 제어한다.

## 주의점

- `galaxy.portrait-video`는 `SM-S936N`, 갤럭시 카메라 `17.0.00.43`, 한국어 UI에서 검증한 선택자를 사용한다. 선택자 변경은 실기기에서 확인한 리소스 ID와 상태 신호에 근거해야 한다(`SKILLS/camera-ui-diagnostics`).
- `runs/`에는 단말 화면과 로그가 저장되므로 Git에서 제외된다. 커밋하지 않는다.
- 문서와 스킬은 한국어로 작성되어 있다.
