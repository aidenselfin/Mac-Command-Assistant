import asyncio, json, os, sys, time
from pathlib import Path
sys.path.insert(0, "/Users/juhyeon/voice-action-AI")
from main import _build_mcp_manager, print_outcome
from rich.console import Console
from openai import AsyncOpenAI
from voice_action.config import load_config
from voice_action.planner import Planner, OpenAICompatibleModel, ApprovalRequired, Clarification
from voice_action.runtime import RuntimeController
from voice_action.undo import UndoJournal
ROOT=Path(__file__).resolve().parent
os.chdir(ROOT)
config=load_config({"allowed_directories":[str(ROOT)]}, cwd=Path("/Users/juhyeon/voice-action-AI"))
console=Console(width=120)
records=json.loads((ROOT/"results.json").read_text())
async def main():
    print("CONFIG",config.provider,config.model,flush=True)
    manager=_build_mcp_manager(config)
    journal=UndoJournal.default(config.allowed_directories)
    async with manager.connect():
        async with AsyncOpenAI(api_key=config.api_key,base_url=config.base_url,timeout=config.llm_timeout_seconds,max_retries=0) as client:
            planner=Planner(config,manager,OpenAICompatibleModel(client,config.model),emit=lambda s: print(s,flush=True),tools=await manager.list_tools(),undo_journal=journal)
            controller=RuntimeController(planner)
            async def run(command, approve=False, choice=None):
                print("\nCOMMAND",command,flush=True)
                start=time.monotonic()
                try:
                    outcome=await controller.submit(command)
                except Exception as exc:
                    from voice_action.planner import Error
                    outcome=Error(str(exc))
                print_outcome(console,outcome)
                stages=[type(outcome).__name__]
                if isinstance(outcome,Clarification):
                    outcome=await controller.submit(choice or "취소")
                    print_outcome(console,outcome);stages.append(type(outcome).__name__)
                if isinstance(outcome,ApprovalRequired):
                    outcome=await (controller.approve_pending() if approve else controller.reject_pending())
                    print_outcome(console,outcome);stages.append(type(outcome).__name__)
                row={"command":command,"seconds":round(time.monotonic()-start,2),"stages":stages,"result":getattr(outcome,"text",getattr(outcome,"message",""))}
                records.append(row)
                (ROOT/"results.json").write_text(json.dumps(records,ensure_ascii=False,indent=2))
                print("ELAPSED",row["seconds"],flush=True)
            commands=[
                ("이 폴더의 파일 목록을 보여줘",False,None),
                ("회의록 파일을 모두 찾아줘",False,None),
                ("회의록-0928.md를 핵심 결정과 미결정 사항 중심으로 3줄 요약해줘",False,None),
                ("회의록-0928.md에서 담당자별 할 일과 마감일을 표로 정리해줘",False,None),
                ("회의록-0928.md와 회의록-0929.md를 읽고 변경된 결정과 일정만 비교해줘",False,None),
                ("expenses.csv를 읽고 총 지출과 분류별 합계를 계산해줘",False,None),
                ("product.md를 읽고 제품 소개를 영어로 번역해줘. 원문에 없는 정보는 추가하지 마",False,None),
                ("product.md에서 배터리 사용일, 충전 시간, 운동 종류 수를 추출하고 가격은 없으면 정보 없음이라고 표시해줘",False,None),
                ("회의록 요약해줘",False,"1"),
                ("회의록-0929.md의 할 일과 마감일을 정리해서 tasks.md에 저장해줘",True,None),
                ("tasks.md 내용을 읽어줘",False,None),
                ("https://example.com 페이지를 읽고 한국어로 2줄 요약해줘",False,None),
                ("https://example.com 페이지를 읽고 web-summary.md에 한국어 요약을 저장해줘",True,None),
                ("현재 활성 앱을 알려줘",False,None),
                ("없는문서-xyz.md를 요약해줘",False,None),
                ("product.md를 renamed.md로 이름 변경해줘",False,None),
                ("승인테스트.txt에 테스트 성공이라고 저장해줘",False,None),
                ("undo-target.txt에 복원 검증이라고 저장해줘",True,None),
            ]
            await run('회의록-0929.md를 읽고 담당자가 명시된 미완료 작업만 담당자, 작업, 마감일 표로 tasks-verified.md에 저장해줘. 날짜는 원문의 월/일 그대로 쓰고 연도를 추가하지 마. 결정사항을 할 일로 바꾸지 마',True,"2")
            await run('product.md를 읽고 핵심 기능을 3줄로 요약해줘',False)
            await run('product.md를 읽고 가격과 배터리 보증기간이 적혀 있는지 확인해줘. 없는 항목은 정보 없음으로 표시해줘',False)
            await run('https://example.com 을 web__browser_navigate로 방문하고 web__browser_snapshot으로 본문을 읽은 다음 한국어로 요약해줘',False)
            await run('delete-check.txt에 삭제 복원 테스트라고 저장해줘',True)
            await run('delete-check.txt 파일을 삭제해줘',True)
            print("UNDO",flush=True)
            print_outcome(console,await controller.undo_last_write())
            print("UNDO_TARGET_EXISTS",(ROOT/"delete-check.txt").exists(),flush=True)
            print("REJECTED_TARGET_EXISTS",(ROOT/"승인테스트.txt").exists(),flush=True)
asyncio.run(main())
