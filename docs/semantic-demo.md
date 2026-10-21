# EmbeddingGemma 체험 자료

`voice-action-test/embedding-demo/`에 외부 사진 3장과 가상 텍스트 문서 3개를 넣었다.
파일명에는 내용 단서를 넣지 않았다. 이 안내는 검색 폴더 밖에 두어 정답표 자체가 검색되는 것을 피한다.

## 먼저 켜기

현재 기본 `.venv`에는 `torch`·`sentence-transformers`가 설치되어 있지 않다.
아래 명령으로 선택 의존성을 설치한다. 최초 사용에는 모델 다운로드와 색인 시간이 필요하다.

```sh
cd /Users/juhyeon/voice-action-AI
uv sync --locked --extra semantic
```

`.voice-action/config.env`에 다음 줄을 추가하거나 기존 off를 on으로 바꾼다. 다른 키는 유지한다.

```dotenv
VOICE_ACTION_SEMANTIC_SEARCH=on
```

열려 있는 앱을 완전히 종료한 후 다시 실행한다.

```sh
./native-preview/run.sh
```

터미널로 먼저 확인하려면 다음을 사용한다.

```sh
uv run --locked --extra semantic python main.py --interactive
```

첫 검색에 "모델을 불러오는 중" 또는 "색인이 일부만 준비됨"이 나오면 잠시 후 다시 요청한다.
파일을 추가한 채 앱을 계속 켜 뒀다면 재색인 주기는 최대 60초다. 재시작하면 새 자료를 함께 색인한다.

## 복사해서 쓸 명령

| 기대 후보 | 명령 |
|---|---|
| asset-101.png | `embedding-demo 폴더에서 철골 뼈대가 드러난 공사 현장 사진을 찾아줘` |
| asset-102.png | `embedding-demo 폴더에서 벽에 액자가 많고 나무 테이블이 있는 카페 사진을 찾아줘` |
| asset-103.png | `embedding-demo 폴더에서 몸을 둥글게 말고 쉬고 있는 고양이 사진을 찾아줘` |
| note-201.txt | `embedding-demo 폴더에서 보안 사고 대응 절차를 다룬 문서를 찾아줘` |
| note-202.txt | `embedding-demo 폴더에서 신규 직원 온보딩 가이드를 찾아줘` |
| note-203.txt | `embedding-demo 폴더에서 반품과 환불 정책 문서를 찾아줘` |

찾은 텍스트 후보를 선택한 뒤에는 `이 문서의 핵심 내용을 세 줄로 요약해줘`도 시험할 수 있다.
이미지는 내용으로 찾는 것까지 지원한다. 이미지 내용을 채팅 모델이 읽고 설명하는 기능은 아니다.

## 무엇을 확인하나

- 파일명·폴더·본문에 같은 검색어가 있으면 일반 검색이 먼저 동작한다.
  모델이 대체어를 넓게 고르면 문서가 일반 검색으로 발견될 수도 있다.
- 텍스트 예시는 검색어를 그대로 반복하지 않고 다른 표현으로 설명했다.
  순수 의미 검색을 확인하려면 터미널의 검색 결과 `stage`가 `meaning`인지 확인한다.
- 의미 검색 결과는 정답 확정이 아니라 후보다. 한 개여도 선택을 요청하는 것이 정상이다.
- 위 표는 기대 후보이며 실제 임베딩 순위·임계값 통과는 아직 측정하지 않았다.
  이번 작업에서는 의존성 설치·모델 다운로드·유료 LLM 호출·운영 색인 변경을 하지 않았다.
- 부정 예: `embedding-demo 폴더에서 스키장에서 스노보드 타는 사람 사진을 찾아줘`.
  해당 사진은 없으며, 유사도가 낮은 파일을 억지로 정답이라고 설명하면 안 된다.
- PDF·DOCX는 현재 의미 검색에서 본문을 읽지 않고 경로만 사용한다.
  문서 내용 검색 체험용으로 UTF-8 `.txt`를 사용했다.

## 사진 출처와 라이선스

2026-10-21 Wikimedia Commons에서 내려받았다. 긴 변을 1280px로 줄이고 PNG로 변환했으며
파일명만 중립적인 이름으로 바꿨다. 추가 생성 이미지는 체험 자료에 사용하지 않았다.

| 로컬 파일 | 원본·저작자 | 라이선스 |
|---|---|---|
| asset-101.png | [Steel frame structure.jpg](https://commons.wikimedia.org/wiki/File:Steel_frame_structure.jpg), Wikideas1 | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| asset-102.png | [Cafe Interior - geograph.org.uk - 4801988.jpg](https://commons.wikimedia.org/wiki/File:Cafe_Interior_-_geograph.org.uk_-_4801988.jpg), Bob Harvey / Geograph | [CC BY-SA 2.0](https://creativecommons.org/licenses/by-sa/2.0/), 변환본도 동일 라이선스 |
| asset-103.png | [Sleeping cat cimg0037.jpg](https://commons.wikimedia.org/wiki/File:Sleeping_cat_cimg0037.jpg), Cruzane | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |

문서 세 개는 이 저장소의 의미 검색 체험을 위해 새로 작성한 가상 자료다.
