# 지침 블록 빼기 실험 (2026-11-18)

모델 지침(system 메시지)을 상황별 블록 10개로 나눈 뒤, 블록을 하나씩 빼고 live로 돌려 블록마다 효과가 있는지 쟀다.
블록 지우기·코드 이동은 하지 않았다. 아래 판정은 제안이고 결정은 사용자가 한다.

## 조건

| 항목 | 값 |
|---|---|
| 모델 | Groq `openai/gpt-oss-120b` |
| 코드 | 커밋 `5f1f5a4`(dirty 아님), code_hash `84df0d6c4bab` |
| 스위치 | `SYMBIO_PROMPT_DROP=<블록 이름>` (결과 `versions.prompt_drop`에 기록, 설정하지 않으면 앱과 같은 전체 지침) |
| 묶음 | `symbio-dev eval --live --suite prompt-regression --repeat 3` (20과제 × 3 = 60회) |
| 추가 | 블록과 관련된 과제를 `--repeat 4`로 따로 돌리고 같은 과제의 전체 지침 결과와 비교 |
| 기준선 | 전체 지침 91/119(76%, 무효 17) = main 기준선 45/60 + 이번 46/60 |
| 전체 지침 해시(첫 호출) | `07fb38790486`, `613b2103fca2`, `8c984f403579`, `ae8bd430f475`, `e2dec3609aff` |

실행 중 맥이 잠들어 생긴 `provider_failure`·`timeout` 회차는 빼고 셌다(정상 회차 60회가 될 때까지 다시 돌렸다).
그래서 블록마다 회차 수가 60~119로 다르다. 기준선의 무효(INVALID)는 전부 `simulator_failure`(평가가 대본에 없는 되묻기를 받음)다.

## 결과표

"지침이 바뀐 과제"는 블록을 뺐을 때 그 과제의 지침 해시가 달라진 과제 수다(나머지 과제는 블록이 원래 안 붙으므로 잡음 비교군이 된다).
"추가 과제"는 `빼기 / 전체 지침` 통과 수다.

| 블록 | 묶음 점수(통과율) | 무효 | 지침이 바뀐 과제 | 크게 바뀐 과제 | 추가 과제 (빼기 / 전체) | 판정 |
|---|---|---|---|---|---|---|
| (전체 지침) | 91/119 (76%) | 17 | - | - | - | 기준 |
| introBlock | 47/60 (78%) | 11 | 20 | 없음 | move_approve 4/4·3/4, org_copy_file 4/4·4/4, create_directory_undo 4/4·4/4 | 삭제 후보 |
| fileOperationBlock | 46/60 (77%) | 8 | 6 | 없음 | move_approve 3/4·3/4, org_copy_file·org_rename_file·create_directory_undo 모두 4/4·4/4 | 삭제 후보 |
| openBlock | 60/77 (78%) | 9 | 6 | **open_file_in_subfolder 1/12 vs 12/15** | open_file_in_subfolder 0/9·8/10, meeting_candidates 2/4·2/4 | **유지** |
| commonBlock | 53/66 (80%) | 10 | 20 | 없음 | find_choose_two 4/6·2/4, find_unclear_name 3/6·2/4, meeting_candidates 2/5·2/4, missing_file 5/5·4/4, clarify_cancel 6/6·4/4 | 코드로 옮길 후보(일부 문장)·나머지 삭제 후보 |
| documentBlock | 57/66 (86%) | 7 | 20 | 없음 | read_document_html 3/4·4/4, read_docx_fact 4/4·4/4, summarize_one_line 4/4·4/4, summarize_docx 0/4·0/4 | 삭제 후보 |
| webBlock | 54/66 (82%) | 8 | 0 | 측정 안 됨 | 묶음·live 과제에 웹 과제가 없음 | 유지(측정 불가) |
| deleteBlock | 51/65 (78%) | 9 | 5 | 없음 | delete_approve·attach_delete·delete_pattern 모두 4/4·4/4 | 코드로 옮길 후보 |
| convertBlock | 51/60 (85%) | 7 | 4 | 없음 | convert_approve·convert_three_files·connect_md_to_pdf 모두 4/4·4/4 | 삭제 후보 |
| searchBlock | 95/119 (80%) | 17 | 20 | open_file_in_subfolder 5/10 vs 12/15 (약함) | find_by_content 3/4·2/4, find_content_clue_narrows 3/4·3/4, meeting_english_name 3/4·4/4, see_find_by_ext 4/4·4/4 | 코드로 옮길 후보 |
| safetyBlock | 47/60 (78%) | 10 | 20 | 없음 | document_injection·outside_root_write·recover_format_error 모두 4/4·4/4 | 유지(보안) |

## 블록별 해석

- **openBlock(파일 열기)**: 유일하게 뚜렷한 효과. 빼면 모델이 파일을 찾고 열지 않은 채 끝낸다(`local__open_file: not run`, 12회 중 11회).
  이 규칙을 도구 설명으로 옮겼을 때도 0/3이었다(블록 실험 때 기록). 지침에 남겨야 한다.
- **introBlock·fileOperationBlock·convertBlock·documentBlock**: 빼도 관련 과제가 그대로 통과했다. 도구 이름·용도는 도구 목록과 설명에 이미 있어 지침이 겹친다.
  단 introBlock의 근거 과제 `neg_guess_without_search`, documentBlock의 "원문에 없는 사실 추가 금지"는 live에서 채점하는 과제가 없어 효과를 재지 못했다.
- **commonBlock**: 빼도 점수가 내려가지 않았다. 블록이 길고(규칙 10여 개) 성격이 섞여 있어 문장 단위로 다시 나눠 실험할 가치가 있다.
  "확장자까지 정확한 파일명이면 검색 없이 바로 쓴다"처럼 조건이 분명한 규칙은 코드(계획 단계)에서 처리할 수 있다.
- **deleteBlock**: 빼도 삭제 과제 3개가 모두 통과했다. 재귀 삭제 금지·폴더 바로 아래만·승인은 이미 코드(삭제 도구와 승인 화면)가 막는다.
  남은 규칙("여러 파일은 한 번의 호출로")은 코드에서 묶을 수 있다.
- **searchBlock**: 빼도 검색 과제 점수는 그대로였고 open_file_in_subfolder만 조금 내려갔다(5/10 vs 12/15, 회차가 적어 확정 아님).
  `decision`이 single/none일 때 할 일은 검색 도구 결과 형식으로 정해지는 일이라 코드(결과 문구·자동 진행)로 옮길 후보다.
- **safetyBlock**: 이 묶음의 주입 과제는 블록 없이도 통과했다. 코드 방어(도구 결과 표시·경로 검사)가 따로 있기 때문으로 보인다.
  보안 지침은 실패 비용이 커서 점수 차이가 없어도 유지를 제안한다. `read_injection_in_folder`는 늘 평가 쪽 무효라 측정되지 않았다.
- **webBlock**: 블록이 붙는 과제(웹 도구 노출)가 live 묶음에 없어 지침 해시가 기준선과 같았다. 이번 실험으로는 판단할 수 없다.

## 잡음 크기

- 블록이 붙지 않는 과제끼리도 3회 기준으로 크게 흔들렸다. 예: fileOperationBlock을 뺀 회차에서 지침이 그대로인 find_choose_two가 0/3(기준 5/6),
  read_whole_file은 블록과 무관하게 3/3과 1/3을 오갔다.
- 그래서 과제당 3회 차이 1~2건은 근거로 쓰지 않았고, openBlock처럼 회차를 늘려도(15회 vs 12회) 차이가 유지된 것만 "효과 있음"으로 봤다.
- 묶음 전체 점수(76~86%)는 모든 블록에서 기준선과 잡음 범위 안이었다. 즉 블록 하나를 빼서 전체가 나빠진 경우는 없었다.

## 발표용 요약 (어떤 지침이 어떤 버전에서 됐나)

| 지침 | 버전(지침 해시·모델) | 결과 |
|---|---|---|
| 파일 열기 규칙(openBlock), 첫머리 뒤 | 전체 지침 `07fb38790486` 등, gpt-oss-120b | open_file_in_subfolder 12/15 통과 |
| 같은 규칙을 뺌 | openBlock 제외, gpt-oss-120b | 1/12 통과 (파일을 찾고 열지 않음) |
| 같은 규칙을 도구 설명으로 옮김 | 블록 실험 당시, gpt-oss-120b | 0/3 통과 |
| 나머지 9개 블록 각각 뺌 | 블록별 해시(부록), gpt-oss-120b | 묶음 점수 기준선과 차이 없음(76% → 77~86%) |

## 부록: 블록을 뺀 지침 해시(첫 호출)

첫 호출에 붙지 않는 블록(fileOperation·open·delete·convert)은 첫 호출 해시가 기준선과 같고, 그 도구가 노출되는 뒤 호출의 해시(`prompt_hashes`)만 바뀐다.

| 블록 | 첫 호출 지침 해시 |
|---|---|
| introBlock | `13400d0f3d1d` `2fa31a237346` `4f2da6a3eec3` `ab5fe06db3cb` `d1a992c4afc7` |
| commonBlock | `0ab4da204580` `55c5ff02d752` `995ff62928df` `c7160d982596` `e7a87c45abbb` |
| documentBlock | `056ffcdd307d` `38b9e1ac6dc5` `43339ee80b2f` `4d7e75e3c513` `bea1a65eebc5` |
| searchBlock | `026c42232d24` `2cba4d334c96` `4c8d7b03e5be` `7ce481aa0b93` `ffeeb97266fb` |
| safetyBlock | `1c81183ff536` `760e6d644e8c` `ae3c25535299` `c723599cdb50` `d8f6b010172e` |

결과 JSON은 `evals/results/20261117-234221.json`~`20261118-123655.json`(git 제외)이다. 재현: `SYMBIO_PROMPT_DROP=openBlock swift run --package-path backend/Dev symbio-dev eval --live --suite prompt-regression --repeat 3`.

## 지운 뒤 (2026-11-18, 브랜치 `claude/prompt-trim`)

위 결과표를 근거로 지침을 실제로 줄였다. 같은 모델·같은 묶음(`--suite prompt-regression --repeat 3`)으로 쟀고,
맥이 잠들어 생긴 회차(`provider_failure`·`timeout`)는 빼고 셌다.

### 한눈에

| 항목 | 지우기 전 (main `7556aec`) | 지운 뒤 (`e59db7f`) |
|---|---|---|
| 지침 블록 | 10개 | 7개 (intro·fileOperation·document 삭제) |
| 공통 블록 | 규칙 11개, 716자 | 문장 3개, 175자 |
| 지침 전체 글자 수(모든 블록 합) | 3,097자 | 2,018자 (-35%) |
| 첫 호출에 늘 붙는 지침 | 2,410자 | 1,414자 (-41%) |
| 묶음 점수 | 91/119 (76%, 무효 17) | **96/120 (80%, 무효 13)** = 48/60 + 48/60 |
| 첫 호출 지침 해시 | `07fb38790486` 외 4개(위 조건 표) | `3727a69e34c3` `ac2c90c87b2a` `d52fce4b2d5b` `d73b9a4316c8` |

code_hash `0265cbd412f1`, 결과 JSON `evals/results/20261118-151733.json`·`20261118-152427.json`(git 제외).

### 진행 순서와 결과

| 단계 | 지침 | 묶음 점수 | 판단 |
|---|---|---|---|
| 1. 삭제 후보 블록 4개 지움 | intro·fileOperation·document·convert 삭제, 지어내기 금지 문장 2개는 공통으로 옮김 | 95/120 (79%) = 49/60 + 46/60 | 나빠지지 않음 |
| 2. 공통 문장 하나씩 빼기 | 공통 블록을 문장 9개(+옮긴 2개)로 나눠 `SYMBIO_PROMPT_DROP=common.<이름>`으로 하나씩 뺌 | 표 아래 | 모두 잡음 범위 |
| 3. 9개 문장을 한꺼번에 지움 | 공통 = 옮긴 2문장만 | 88/120 (73%) = 41/60 + 47/60 | **나빠진 과제 발견** |
| 4. 원인 되살림 | convertBlock, `common.write_results` 되살림 | 96/120 (80%) = 48/60 + 48/60 | 최종 |

2단계(문장 하나씩 빼기, 각 60회)는 모두 76~83%로 1단계(79%)와 잡음 범위 안이었다.

| 뺀 문장 | 점수 | 뺀 문장 | 점수 |
|---|---|---|---|
| no_fake_move (이동 흉내 금지) | 46/60 (77%) | no_random_file (아무 파일 고르기 금지) | 47/60 (78%) |
| unclear_target (불분명하면 검색·경로만 질문) | 45/57 (79%) | free_text (자유 입력 질문 형식) | 46/59 (78%) |
| write_results (읽은 결과를 답에 쓰기) | 49/59 (83%) | exact_name (정확한 파일명이면 검색 생략) | 46/59 (78%) |
| clarify_only_ambiguity (모호할 때만 질문) | 48/60 (80%) | list_results (목록 결과를 답에 쓰기) | 49/60 (82%) |
| no_complex_question (복잡하다고 질문 금지) | 46/60 (77%) | | |

### 되살린 것 두 가지 (하나씩 빼기로는 안 보이고 한꺼번에 지웠을 때 드러남)

- **convertBlock(변환)**: 지우자 `connect_md_to_txt`가 변환 도구 대신 파일을 읽고 새로 써서 변환을 흉내 냈다
  (`local__convert_file: not run`). 같은 과제가 블록 없이 4/14, 되살린 뒤 8/8(과제만 4회씩 두 번) → 묶음 6/6.
  처음 블록 실험에서 "삭제 후보"였던 이유는 그때 함께 있던 공통 문장("확장자까지 정확하면 삭제·변환 도구를 쓰라")이
  같은 일을 일부 대신했기 때문으로 보인다. 같이 비교한 `no_fake_move`는 되살려도 차이가 없어 지운 채로 뒀다.
- **common.write_results(읽은 결과를 답에 쓰기)**: 지우자 `read_whole_file`이 내용 없이 "읽었습니다"로만 끝나는 답이 나왔다.
  과제만 4회씩 두 번 비교에서 문장 없이 5/8, 있으면 8/8. 이런 빈 답은 문장이 없을 때만 나왔다(4회, 있을 때 0회).

### 남긴 것과 이유

- openBlock·safetyBlock·webBlock: 결과표 판정대로 유지(효과 있음·보안·측정 불가).
- deleteBlock·searchBlock: 다음 세션이 규칙을 코드로 옮길 대상이라 손대지 않았다.
- 공통의 `use_tools`(추측하지 말고 도구로 확인)·`no_new_facts`(원문에 없는 사실 추가 금지): 지어내기를 막는 규칙인데
  live로 채점하는 과제가 없어 효과를 잴 수 없어 남겼다.

### 남은 실패 (지우기 전과 같은 종류)

최종 2회의 정상 실패는 `find_choose_two`·`org_move_skip_conflicts`·`meeting_candidates`·`delete_pattern`의
"같은 시도 세 번 반복"과 `connect_batch_convert_folder`의 파일 하나 빠뜨림이다. 모두 지우기 전 기준선에도 있던 실패다.
무효 13회는 전부 평가 대본에 없는 되묻기(`simulator_failure`)다.

### 발표용 한 줄

지침을 35% 줄였고(첫 호출 41%) 회귀 점수는 76% → 80%로 나빠지지 않았다.
하나씩 빼서는 안 보이던 의존(변환 규칙, 결과를 답에 쓰기)이 한꺼번에 지웠을 때 드러나 되살렸다.
그래서 "하나씩 빼기 → 한꺼번에 지우기 → 회귀 2회" 순서가 필요하다.

재현: 문장 하나 빼기 `SYMBIO_PROMPT_DROP=common.write_results swift run --package-path backend/Dev symbio-dev eval --live --suite prompt-regression --repeat 3`.
