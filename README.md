# 갤럭시 카메라 테스트

회사 테스트폰에서 원하는 모드를 열거나 촬영하고, 사진이나 동영상이 저장됐는지 확인하는 도구입니다.

## 사내 Agent에 모드 하나 요청하기

테스트폰이 연결된 **회사 PC에서 명령을 실행할 수 있는 에이전트**에게 아래 한 줄을 복사해 보내세요.

```text
https://github.com/TTolsun/camera-test 참고해서 Pro Video 동작시켜 줘.
```

이렇게 요청하면 후면 카메라로 프로 동영상을 5초간 찍고, 저장된 MP4를 검사합니다. 카메라 화면만 보고 싶다면 **“전면 Pro Video 열어 줘”**라고 하세요.

### 다음에는 하고 싶은 일만 말하세요

| 하고 싶은 일 | 복사해서 보낼 문장 |
| --- | --- |
| 이 단말을 처음 검사할 때 | 이 단말의 전·후면 모드를 검사해서 등록해 줘. |
| 두 방향을 비교할 때 | Pro Video를 후면과 전면에서 각각 3초씩 찍고 결과를 등록해 줘. |
| 다른 모드를 쓸 때 | 야간 모드로 사진 한 장 찍어 줘. |
| 순서대로 찍을 때 | 후면 Pro Video로 5초 찍고, 전면 일반 동영상으로 3초 찍어 줘. |
| 지난 결과만 볼 때 | 이 단말에서 촬영까지 확인한 모드를 알려 줘. |

전체 검사는 몇 분 걸릴 수 있습니다. 카메라를 **사진 모드**에 두고 검사가 끝날 때까지 화면을 켜 두세요. 파노라마처럼 사람이 움직여야 하는 모드는 결과에 따로 표시됩니다.

## 회사 PC에서 처음 연결할 때

이미 연결을 마쳤다면 위의 요청부터 바로 써도 됩니다.

1. **PC를 준비하세요.** Python 3.10 이상과 ADB가 필요합니다. ADB는 PC에서 테스트폰에 명령을 보내는 도구입니다. [Android SDK Platform-Tools 받기](https://developer.android.com/tools/releases/platform-tools)
2. **테스트폰을 연결하세요.** 휴대전화의 개발자 옵션에서 USB 디버깅을 켜고, USB 케이블을 꽂은 뒤 휴대전화에 뜨는 허용 버튼을 누르세요.
3. **PC에서 연결을 확인하세요.** 처음이라면 터미널에서 아래 세 줄을 차례로 실행하세요. 이미 저장소를 받았다면 그 폴더에서 마지막 줄만 실행하면 됩니다.

```powershell
git clone https://github.com/TTolsun/camera-test.git
cd camera-test
python camera-test.pyz doctor
```

**‘실행할 준비가 되었습니다’**가 나오면 테스트를 시작하세요. `[FAIL]`이 보이면 그 아래 화살표(`->`)에 적힌 조치부터 하세요.

<details>
<summary>USB 대신 무선으로 연결하려면</summary>

PC와 테스트폰을 같은 Wi-Fi에 연결하세요. 휴대전화의 **개발자 옵션 → 무선 디버깅 → 페어링 코드로 기기 페어링**에서 주소와 코드를 확인하세요. PC에서 `adb pair <IP>:<페어링 포트>`를 실행하고 코드를 입력하세요. 이후 `python camera-test.pyz doctor`의 연결 안내를 따르세요.

</details>

<details>
<summary>에이전트 없이 직접 실행하려면</summary>

저장소 폴더에서 필요한 명령 하나를 골라 실행하세요. 배포 파일 `camera-test.pyz`만 받았다면 그 파일이 있는 폴더에서 실행하면 됩니다.

```powershell
# 전·후면 모드 전체 검사 및 등록
python camera-test.pyz suite --register-device

# 전면 Pro Video 3초 녹화 및 저장 확인
python camera-test.pyz mode "Pro Video" --facing front --duration 3 --register-device

# 이 단말에서 지금까지 확인한 결과 조회
python camera-test.pyz capabilities
```

</details>

## 결과에서 무엇을 보면 되나요?

- **`OPENED`**면 요청한 모드와 카메라 방향을 화면에서 확인한 것입니다.
- **`PASS`**면 해당 촬영 검사에서 새 사진이나 동영상의 저장까지 확인한 것입니다.
- **`UNVERIFIED_FACING`**이면 요청한 방향을 확인하지 못했습니다. 예를 들어 전면을 요청했는데 카메라가 후면으로 돌아간 경우입니다. 다음에 할 일은 결과의 `next_action`에 나옵니다.

같은 회사 PC에서 **같은 폴더**를 계속 쓰세요. 그래야 단말과 카메라 앱 버전별로 기록이 쌓입니다. 전체 검사 보고서는 `runs/suite/<실행 ID>/report.md`에 있습니다. `runs/`에는 촬영물과 로그도 들어가므로 사내 PC에 보관하세요.

## 더 자세한 가이드

- 새 카메라 빌드를 검사하거나 랩 단말을 운영할 때는 [운영 가이드](docs/operations.md)를 보세요.
- 명령 옵션과 오류 코드의 뜻은 [명령·결과 참조](docs/reference.md)에서 찾을 수 있습니다.
- 에이전트가 요청을 어떤 명령으로 실행하는지는 [에이전트 지침](AGENTS.md)과 [카메라 테스트 스킬](SKILLS/camera-test/SKILL.md)에 있습니다.
- Camera HAL 로그를 비교하려면 [HAL 검사 가이드](docs/exynos-hal-validation.md)를, 도구를 수정하려면 [개발 가이드](CLAUDE.md)를 보세요.

기존 실기기 검증 환경은 SM-S936N, Android 17, 갤럭시 카메라 17.0.00.43(한국어)입니다. 회사 테스트폰의 결과는 그 단말에서 검사한 뒤 확인할 수 있습니다.
