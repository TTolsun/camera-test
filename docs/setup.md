# 설치와 단말 준비

이 문서는 PC에 도구를 준비하고 테스트 단말을 ADB로 연결하는 절차를 설명합니다. 한 번만 하면 됩니다.

## 1. PC 준비

1. Python 3.10 이상을 설치합니다. `python --version`으로 확인합니다.
2. [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)를 설치합니다. Android Studio를 이미 설치했다면 포함되어 있습니다.
3. 저장소를 받습니다. 외부 패키지 설치는 필요하지 않습니다.

   ```powershell
   git clone https://github.com/TTolsun/camera-test.git
   cd camera-test
   ```

도구는 ADB를 다음 순서로 찾습니다. 찾지 못하면 `ADB_NOT_FOUND` 오류가 나며, 이때는 `CAMERA_TEST_ADB`에 경로를 지정합니다.

1. 환경 변수 `CAMERA_TEST_ADB`
2. PATH의 `adb`
3. `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `%LOCALAPPDATA%\Android\Sdk` 아래의 `platform-tools`

```powershell
$env:CAMERA_TEST_ADB = "C:\Users\<사용자>\AppData\Local\Android\Sdk\platform-tools\adb.exe"
```

## 2. 단말 준비

1. **개발자 옵션**을 켭니다. 설정 > 휴대전화 정보 > 소프트웨어 정보에서 빌드번호를 7번 누릅니다.
2. 개발자 옵션에서 **USB 디버깅** 또는 **무선 디버깅**을 켭니다.
3. 카메라 앱 버전을 확인합니다. 검증된 버전은 `17.0.00.43`입니다. 다른 버전이면 [새 앱 빌드 대응](new-app-build.md)을 따릅니다.
4. 저장 공간이 500MB 이상 남아 있어야 합니다. 부족하면 `PRECHECK_LOW_STORAGE`로 실행 전에 중단합니다.

## 3. 연결

### USB

케이블을 연결하고, 단말에 뜨는 "USB 디버깅 허용"을 승인합니다.

```powershell
adb devices -l
```

목록에 `device` 상태로 보이면 연결된 것입니다. `unauthorized`이면 단말 화면에서 승인합니다.

### 무선 디버깅

PC와 단말이 같은 네트워크에 있어야 합니다.

1. 단말의 개발자 옵션 > 무선 디버깅 > **페어링 코드로 기기 페어링**을 누릅니다. 화면에 IP 주소, 포트, 6자리 코드가 표시됩니다.
2. PC에서 페어링합니다. 코드를 묻는 프롬프트에 단말에 표시된 코드를 입력합니다.

   ```powershell
   adb pair <IP>:<페어링 포트>
   ```

3. 무선 디버깅 첫 화면의 **IP 주소 및 포트**(페어링 포트와 다릅니다)로 연결합니다.

   ```powershell
   adb connect <IP>:<포트>
   adb devices -l
   ```

무선 디버깅의 연결 포트는 단말을 재부팅하거나 무선 디버깅을 껐다 켜면 바뀝니다. 포트를 모를 때는 `adb mdns services`로 찾을 수 있습니다.

> 무선 디버깅에서는 한 단말이 `IP:포트`와 `adb-<일련번호>-..._adb-tls-connect._tcp` 두 항목으로 나열되기도 합니다. 도구는 하드웨어 일련번호가 같으면 한 대로 보고 `IP:포트` 쪽을 자동으로 고릅니다. 실제로 단말이 여러 대 연결되어 있을 때는 `--device <serial>`로 지정합니다.

## 4. 연결 확인

```powershell
python camera-test.pyz inspect --pretty
```

다음 값을 확인합니다.

| 필드 | 기대값 |
|---|---|
| `device.model`, `device.soc_vendor` | 연결한 단말의 모델과 SoC(`qualcomm` 또는 `exynos`)입니다. |
| `device.camera_version` | 카메라 앱 버전입니다. |
| `scenario.status` | `runnable`이면 바로 실행할 수 있습니다. `unverified`이면 앱 버전이 검증 목록에 없습니다. |
| `preflight` | 화면(`screen`), 잠금(`lock`), 여유 공간(`free_bytes`) 측정값입니다. |

`inspect`는 카메라를 열어 모드 목록을 확인한 뒤 원래 모드로 돌아갑니다. 카메라를 열지 않으려면 `--no-ui`를 추가합니다.

## 다음 단계

- 한 번 실행해 보기: [일상 검증과 결과 읽기](daily-use.md)
- 전용 랩 단말로 자동 검증하기: [랩 단말 자동 운영](lab-automation.md)
