# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

작업별 스킬 선택은 [AGENTS.md](AGENTS.md)를 따른다. 사용자용 설명(실행 방법, 종료 코드, 검사 등록)은 [README.md](README.md)에 있다.

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

CLI(`camera_runner/cli.py`)가 `list`, `inspect`, `run`, `profile {show,validate}`, `hal {begin,collect,compare}` 하위 명령을 제공하고, 결과를 JSON으로 stdout에 출력한다. 오류는 `RunError(code, message)`로 올라와 JSON의 오류 코드가 된다.

- **ADB 전송 계층** (`adb.py`): 모든 기기 통신은 모듈 함수 `adb.adb(*args)` 하나를 거친다. `Device`는 그 위에서 shell, UI 덤프, logcat, pull을 제공한다. `device_lock`은 임시 디렉터리 파일 잠금으로 같은 기기에 대한 동시 실행을 막는다(`DEVICE_BUSY`).
- **시나리오 레지스트리** (`scenarios/__init__.py`): `SCENARIOS` dict 하나를 `list`, `inspect`, `run`이 공유한다. 시나리오는 `scan(device, profile)`(모드 탐색)과 `execute(device, profile, seconds, result)`(녹화 후 녹화 시작 시각 반환)를 제공한다.
- **앱 프로필** (`profiles.py`): 시나리오가 의존하는 리소스 ID, UI 문구, 녹화 상태 로그 정규식, `verified_versions`를 데이터로 둔다. 내장 프로필은 시나리오 모듈의 `PROFILE` dict이고, `--profile-file`이 같은 키 집합을 가진 JSON으로 대체한다(키 불일치는 거부). `run`은 앱 버전이 `verified_versions`에 없으면 `--allow-unverified-version` 없이는 조작 전에 중단한다. 실패하면 마지막 UI 덤프(`Device.last_ui`)와 프로필을 비교한 `diagnosis`를 결과에 넣는다.
- **기능 검사** (`checks.py`): `--checks-file` JSON(`schema_version: 1`)을 읽는다. `source: hal`은 `vendor`(`exynos`/`qualcomm`)가 필수이고, `source: app`은 `vendor`가 없어야 한다. 같은 이름에 vendor별 정의를 둘 수 있고, 감지된 SoC로 하나를 고른다. 판정은 녹화 시작 시각 이후 해당 태그 로그에 정규식이 매칭되는지로 한다.
- **HAL 시험** (`hal.py`): `begin`이 기기 시각을 기록한 trial JSON을 만들고, `collect`가 그 이후 logcat을 저장한다. `compare`는 저장된 `threadtime` 로그만 읽으므로 ADB 없이 동작한다.
- **logcat 형식** (`logcat.py`): 태그·시각·`threadtime` 줄 정규식을 수집, 비교, 검사가 공유한다.

## 테스트 방식

테스트는 실기기 없이 `patch.object(adb, "adb", side_effect=MockCameraAdb())`로 ADB 호출 전체를 가짜로 대체하고, `camera_test.main()`을 `sys.argv` 패치로 호출해 stdout JSON을 검사한다. `MockCameraAdb`는 처리하지 않는 ADB 명령에 `AssertionError`를 던지므로, 시나리오가 새 ADB 명령을 쓰면 mock에도 응답을 추가해야 한다.

## 주의점

- `galaxy.portrait-video`는 `SM-S936N`, 갤럭시 카메라 `17.0.00.43`, 한국어 UI에서 검증한 선택자를 사용한다. 선택자 변경은 실기기에서 확인한 리소스 ID와 상태 신호에 근거해야 한다(`SKILLS/camera-ui-diagnostics`).
- `runs/`에는 단말 화면과 로그가 저장되므로 Git에서 제외된다. 커밋하지 않는다.
- 문서와 스킬은 한국어로 작성되어 있다.
