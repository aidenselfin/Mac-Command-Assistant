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
records=[]
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
                    if approve and outcome.operation != "delete":
                        # Only approve the expected test file with source-preserving task content.
                        approve = (Path(outcome.target_path).parent == ROOT
                                   and Path(outcome.target_path).name.startswith("tasks-")
                                   and all(x in outcome.preview for x in ("지연", "현우", "10월 5일", "10월 10일"))
                                   and all(x not in outcome.preview for x in ("2023", "2026", "할인", "민수")))
                    outcome=await (controller.approve_pending() if approve else controller.reject_pending())
                    print_outcome(console,outcome);stages.append(type(outcome).__name__)
                row={"command":command,"seconds":round(time.monotonic()-start,2),"stages":stages,"result":getattr(outcome,"text",getattr(outcome,"message",""))}
                records.append(row)
                (ROOT/"results.json").write_text(json.dumps(records,ensure_ascii=False,indent=2))
                print("ELAPSED",row["seconds"],flush=True)
            for trial in range(1,4):
                await run(f"{ROOT}/expenses.csv를 읽고 총 지출과 분류별 합계를 계산해줘")
                await run(f"{ROOT}/회의록-0929.md를 읽고 담당자가 명시된 미완료 작업만 담당자, 작업, 마감일 표로 tasks-{trial}.md에 저장해줘",True,"2")
                await run("https://example.com 페이지를 읽고 한국어로 2줄 요약해줘")
asyncio.run(main())
