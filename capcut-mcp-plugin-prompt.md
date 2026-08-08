# CapCut MCP 플러그인 개발 프롬프트 (Zero-Dependency)

> 이 문서 전체를 Claude Code 첫 메시지로 붙여넣거나, `SPEC.md`로 저장한 뒤 "SPEC.md 읽고 Phase 0부터 진행해줘"로 시작하세요.

---

## 프로젝트 목표

CapCut(국제판) 로컬 프로젝트를 **읽기/분석**하고, 새 프로젝트를 **draft 파일로 생성**할 수 있는 MCP 서버를 만들고, Claude Code 플러그인으로 패키징한다.

핵심 원칙:
- **의존성 제로.** 외부 패키지 일절 금지. Python 3.11+ 표준 라이브러리만 사용한다. `pip install` 없이 `python server.py`로 실행 가능해야 한다. MCP SDK도 쓰지 않고 프로토콜을 직접 구현한다.
- CapCut에는 공식 편집 API가 없다. 모든 것은 로컬 draft 폴더(`draft_content.json` 등)의 읽기/쓰기로 구현한다.
- 결과물은 "완성 영상"이 아니라 "CapCut에서 열어 편집 가능한 프로젝트"다. 렌더링/export는 스코프 밖.
- raw JSON을 절대 MCP 응답으로 그대로 반환하지 않는다. 서버에서 소화(digest)한 요약을 계층적으로 제공한다.

## 기술 스택

- Python 3.11+ **표준 라이브러리 only** — `json`, `sys`, `pathlib`, `dataclasses`, `shutil`, `tempfile`, `subprocess`(프로세스 체크용), `uuid`, `platform`
- 모델: `@dataclass` (pydantic 금지)
- 테스트: `unittest` (pytest 금지)
- 미디어 길이 감지: ffprobe가 PATH에 있으면 사용, 없으면 `duration_sec` 명시를 요구 (graceful degradation — 필수 의존 아님)
- 패키징: Claude Code 플러그인 형식 (`.claude-plugin/plugin.json` + `.mcp.json`)

## MCP 프로토콜 직접 구현 명세

SDK 없이 stdio 트랜스포트를 직접 구현한다. `protocol.py` 하나로 완결.

- **트랜스포트**: stdin에서 한 줄 = JSON-RPC 2.0 메시지 하나 (개행 구분 JSON, LSP식 Content-Length 헤더 아님). 응답은 stdout에 한 줄로 쓰고 flush. **stdout에는 JSON-RPC 외 어떤 출력도 금지** — 디버그/로그는 전부 stderr로.
- **구현할 메서드**:
  - `initialize` → `{protocolVersion, capabilities: {tools: {}}, serverInfo: {name, version}}` 반환. protocolVersion은 클라이언트가 보낸 값을 그대로 에코.
  - `notifications/initialized` → 알림이므로 응답하지 않음
  - `tools/list` → 툴 정의 배열. 각 툴은 `{name, description, inputSchema}`, inputSchema는 JSON Schema (dict를 손으로 작성)
  - `tools/call` → 디스패치 후 `{content: [{type: "text", text: <결과 JSON 문자열>}]}` 반환. 툴 내부 예외는 `{content: [...], isError: true}`로 감싸서 반환 (JSON-RPC 에러 아님)
  - `ping` → `{}` 반환
  - 미지 메서드 → JSON-RPC 에러 `-32601`
- **툴 등록**: 데코레이터 기반 경량 레지스트리를 직접 작성. 함수 시그니처의 타입 힌트에서 inputSchema를 자동 생성하려 하지 말고, 툴마다 스키마 dict를 명시적으로 선언한다 (마법 금지, 명시가 낫다).
- 파싱 불가능한 입력 줄은 stderr에 기록하고 건너뛴다. EOF에서 정상 종료.

## 시스템 아키텍처

```
┌─────────────────────────────────────────────────┐
│ Claude Code (MCP client)                        │
└──────────────────┬──────────────────────────────┘
                   │ 개행 구분 JSON-RPC 2.0 (stdio)
┌──────────────────▼──────────────────────────────┐
│ protocol.py — JSON-RPC 루프, MCP 핸드셰이크,     │
│               툴 레지스트리 (범용, CapCut 무관)   │
├─────────────────────────────────────────────────┤
│ server.py — 툴 선언 + 스키마 + 서비스 호출        │
├─────────────────────────────────────────────────┤
│ services/                                       │
│  ├ analyzer.py   읽기: 파싱 → 요약/타임라인/자막 │
│  └ builder.py    쓰기: 프로젝트 조립 상태 관리    │
├─────────────────────────────────────────────────┤
│ engine/ (어댑터 레이어)                          │
│  ├ draft_reader.py   draft JSON → 내부 모델      │
│  ├ draft_writer.py   내부 모델 → draft 폴더 생성  │
│  ├ models.py         Project/Track/Segment 등    │
│  │                   (dataclass, CapCut 스키마와  │
│  │                    독립적인 자체 도메인 모델)    │
│  └ locator.py        drafts 경로/버전 자동 감지    │
└──────────────────┬──────────────────────────────┘
                   │ file I/O
┌──────────────────▼──────────────────────────────┐
│ CapCut Drafts 디렉토리                           │
│  Windows: %LOCALAPPDATA%\CapCut\User Data\      │
│           Projects\com.lveditor.draft\          │
│  macOS: ~/Movies/CapCut/User Data/Projects/     │
│         com.lveditor.draft/                     │
└─────────────────────────────────────────────────┘
```

레이어 규칙:
- `protocol.py`는 CapCut을 전혀 모른다. 어떤 MCP 서버에도 재사용 가능한 범용 모듈로 작성.
- `server.py`는 툴 선언 + 스키마 + 서비스 호출만. 비즈니스 로직 금지.
- `services/`는 `engine/models.py`의 도메인 모델만 다룬다. CapCut JSON 스키마를 직접 알면 안 된다.
- CapCut 스키마 지식은 `engine/draft_reader.py`와 `draft_writer.py` 두 파일에만 존재한다. 포맷이 바뀌면 이 두 파일만 고친다.
- 시간 단위: 내부 모델은 전부 **마이크로초(int)**. 툴 입출력은 초(float)로 받고 경계에서 변환.

## MCP 툴 명세

### 읽기 (Phase 2)

| 툴 | 입력 | 출력 |
|---|---|---|
| `list_projects()` | - | `[{name, modified_at, duration_sec, resolution, fps}]` |
| `analyze_project(name)` | 프로젝트명 | 트랙 구성 요약, 클립 수, 총 길이, 사용 미디어 목록(경로+존재 여부), 이펙트/트랜지션 종류별 카운트, 자막 유무 |
| `get_timeline(name, track_type?)` | 트랙 타입 필터 옵션 | `[{track_index, track_type, segment_id, start_sec, end_sec, source_name, speed}]` — 키프레임·이펙트 파라미터 제외한 압축 뷰 |
| `get_captions(name)` | 프로젝트명 | `[{start_sec, end_sec, text}]` |
| `get_segment_detail(name, segment_id)` | 세그먼트 ID | 해당 클립만 풀 디테일(키프레임, 이펙트 파라미터, 볼륨, 변형 포함) |

### 쓰기 (Phase 3)

| 툴 | 설명 |
|---|---|
| `create_project(name, width, height, fps)` | 메모리에 새 프로젝트 상태 생성 (아직 디스크에 안 씀) |
| `add_video(project, path, start_sec, duration_sec?, track?, speed?, volume?)` | 비디오 클립 추가. `duration_sec` 생략 시: ffprobe 있으면 자동 감지, 없으면 명시 요구 에러 |
| `add_audio(project, path, start_sec, duration_sec?, volume?, fade_in?, fade_out?)` | 오디오 트랙 |
| `add_text(project, text, start_sec, duration_sec, font?, size?, color?, position?)` | 텍스트/자막 |
| `add_subtitles_from_srt(project, srt_path)` | SRT 파일 → 자막 트랙 일괄 생성 (SRT 파서도 직접 작성, 정규식으로 충분) |
| `save_draft(project, open_hint?)` | draft 폴더를 tmp에서 완성 → drafts 디렉토리로 **원자적 이동**(rename). 저장 후 "CapCut을 재시작하면 목록에 나타난다"는 안내 문자열 반환 |

### 진단 (Phase 2)

| 툴 | 설명 |
|---|---|
| `doctor()` | drafts 경로 감지 결과, CapCut 실행 중 여부(프로세스 체크: Windows `tasklist` / macOS `pgrep`), 감지된 draft 포맷 버전, ffprobe 가용 여부, 읽기/쓰기 권한을 리포트 |

## 안전 규칙 (구현 필수)

1. **쓰기 전 CapCut 프로세스 체크**: `save_draft`는 CapCut이 실행 중이면 경고를 반환하고 `force=True` 없이는 진행하지 않는다.
2. **기존 프로젝트 수정 금지 (v1)**: 쓰기는 항상 새 폴더 생성만. 기존 draft를 덮어쓰는 툴은 만들지 않는다.
3. **읽기 재시도**: JSON 파싱 실패 시(앱이 저장 중일 수 있음) 0.5초 간격 3회 재시도.
4. **원자적 쓰기**: draft 폴더는 tmp에서 완성 후 최종 위치로 rename.
5. **경로 검증**: 미디어 경로는 절대경로로 정규화하고 존재 여부를 저장 전에 확인, 없으면 목록으로 경고.

## 프로젝트 구조

```
capcut-mcp/
├── .claude-plugin/
│   └── plugin.json          # name, version, description
├── .mcp.json                # mcpServers: {"capcut": {"command": "python", "args": ["src/capcut_mcp/server.py"]}}
├── src/capcut_mcp/
│   ├── protocol.py          # 범용 MCP stdio 서버 (CapCut 무관)
│   ├── server.py
│   ├── services/
│   │   ├── analyzer.py
│   │   └── builder.py
│   └── engine/
│       ├── models.py
│       ├── draft_reader.py
│       ├── draft_writer.py
│       └── locator.py
├── tests/                   # unittest, python -m unittest 로 실행
│   ├── fixtures/            # 실제 CapCut이 만든 샘플 draft JSON 2~3개
│   ├── test_protocol.py     # 핸드셰이크/tools/list/tools/call을 파이프로 E2E 검증
│   ├── test_reader.py
│   ├── test_writer.py
│   └── test_roundtrip.py    # writer로 만든 draft를 reader로 읽어 일치 검증
└── README.md
```

pyproject.toml 없음 — 설치할 게 없다. Python 3.11+만 있으면 된다.

## 구현 순서 (Phase별로 진행하고, 각 Phase 끝날 때 멈춰서 확인받을 것)

**Phase 0 — 정찰**: `locator.py` 먼저. 현재 머신에서 drafts 경로를 찾고, 실제 draft 폴더 하나를 골라 `draft_content.json` 구조를 조사해 발견한 스키마(트랙 구조, 시간 단위, materials 연결 방식, 필수 필드)를 `docs/schema-notes.md`에 기록. 이 기록이 이후 모든 구현의 근거다. 실제 draft가 없으면 사용자에게 CapCut에서 간단한 테스트 프로젝트를 하나 만들어달라고 요청할 것.

**Phase 1 — 프로토콜**: `protocol.py` 구현 + `test_protocol.py`. 더미 툴(`echo`) 하나 등록해서 subprocess 파이프로 initialize → tools/list → tools/call 왕복을 테스트로 검증. 이게 통과하면 `claude mcp add capcut -- python src/capcut_mcp/server.py`로 실제 Claude Code에 붙여 연결 확인.

**Phase 2 — 읽기**: models → draft_reader → analyzer → 읽기 툴 5종 + doctor. 실제 draft로 검증.

**Phase 3 — 쓰기**: draft_writer → builder → 쓰기 툴 6종. 라운드트립 테스트 통과 후, 생성된 draft를 CapCut에서 실제로 열어보는 수동 검증을 사용자에게 요청.

**Phase 4 — 패키징**: plugin.json, .mcp.json, README(설치법: "Python 3.11+ 필요, 그 외 설치 없음", 안전 규칙, 알려진 제약). E2E 확인.

## 제약/알려진 리스크 (README에도 명시)

- draft 포맷은 비공식·리버스 엔지니어링 대상이므로 CapCut 업데이트 시 깨질 수 있음
- 중국판 JianYing 6.x+는 draft 암호화로 지원 불가, 국제판 CapCut 대상
- export 자동화는 미지원 (draft 생성까지)
- CapCut 버전에 따라 `draft_content.json` / `draft_info.json` 파일명이 다를 수 있으므로 locator에서 감지
- ffprobe 부재 시 비디오/오디오 길이 자동 감지 불가 (duration_sec 명시 필요)
