# bstage 재입고 디스코드 알림

비스테이지 상품 페이지를 주기적으로 확인하고, 구매 가능 상태가 감지되면 디스코드 웹훅으로 알려주는 프로그램입니다.

기본 상품:

- https://rolster.bstage.in/shop/products/279

## 포함된 기능

- Playwright 브라우저 기반 확인
- 30초 이상 간격으로 반복 확인
- 구매 버튼 활성화 감지
- 품절 문구 감지
- 재입고 알림 중복 방지
- Railway 재시작 정책
- 디스코드 웹훅 환경변수 사용
- 시작 알림 선택 가능
- 오류 알림 선택 가능

## GitHub에 올리기

1. 이 프로젝트의 압축을 풉니다.
2. GitHub 저장소 `wlgggy/bstage`에 들어갑니다.
3. `Add file` → `Upload files`를 누릅니다.
4. 압축을 푼 파일 전체를 업로드합니다.
5. `.env` 파일은 절대 올리지 않습니다.
6. `Commit changes`를 누릅니다.

## Railway 배포

1. Railway에 로그인합니다.
2. `New Project`를 누릅니다.
3. `Deploy from GitHub repo`를 선택합니다.
4. `wlgggy/bstage` 저장소를 연결합니다.
5. 배포된 서비스에서 `Variables` 메뉴로 들어갑니다.
6. 아래 환경변수를 추가합니다.

### 필수 환경변수

```text
DISCORD_WEBHOOK_URL=새로 만든 디스코드 웹훅 주소
```

### 선택 환경변수

```text
PRODUCT_URL=https://rolster.bstage.in/shop/products/279
CHECK_INTERVAL_SECONDS=60
STARTUP_NOTIFICATION=true
NOTIFY_ON_ERROR=false
HEADLESS=true
LOG_LEVEL=INFO
```

## 디스코드 웹훅 만드는 방법

1. 디스코드 서버에서 알림을 받을 채널로 이동합니다.
2. 채널 설정을 엽니다.
3. `연동` 또는 `Integrations`를 누릅니다.
4. `Webhooks`를 누릅니다.
5. 새 웹훅을 만듭니다.
6. 웹훅 URL을 복사합니다.
7. Railway `Variables`의 `DISCORD_WEBHOOK_URL` 값으로 넣습니다.

## 보안 주의

- 디스코드 웹훅 URL은 비밀번호처럼 취급해야 합니다.
- GitHub 코드나 README에 웹훅 주소를 직접 넣지 마세요.
- 채팅이나 공개 저장소에 올린 웹훅은 삭제하고 새로 발급하세요.
- `.env` 파일은 `.gitignore`에 포함되어 있습니다.

## 동작 방식

프로그램은 기본적으로 60초마다 상품 페이지를 엽니다.

다음 조건 중 하나를 확인합니다.

- 활성화된 `구매하기`
- 활성화된 `바로 구매`
- 활성화된 `장바구니`
- `품절` 또는 유사 문구
- 비활성화된 구매 버튼

구매 가능 상태가 처음 감지되거나, 품절 상태에서 구매 가능 상태로 바뀌면 디스코드 알림을 한 번 보냅니다.

## Railway 로그 확인

Railway 프로젝트에서 서비스 선택 후 `Deployments` 또는 `Logs` 메뉴를 확인합니다.

정상 동작 시 대략 아래와 같은 로그가 표시됩니다.

```text
available=False | 페이지에서 '품절' 문구를 찾았습니다.
```

재입고 감지 시:

```text
available=True | 활성화된 구매 버튼을 찾았습니다.
```

## 주의사항

- 상품 페이지 구조가 바뀌면 감지 코드 수정이 필요할 수 있습니다.
- 로그인이나 특정 옵션 선택이 필수인 상품은 추가 대응이 필요할 수 있습니다.
- 너무 짧은 간격으로 요청하면 사이트에 부담을 줄 수 있으므로 60초 이상을 권장합니다.
