import os, json
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from models import AssistantThreadCreate, AssistantMessageCreate
from database import get_db
from models import AssistantThread, AssistantMessage
from datetime import datetime
from openai import AsyncAssistantEventHandler, AsyncOpenAI, AssistantEventHandler, OpenAI
from openai.types.beta.threads import Text, TextDelta
from openai.types.beta.threads.runs import ToolCall, ToolCallDelta
from openai.types.beta.threads import Message, MessageDelta
from openai.types.beta.threads.runs import ToolCall, RunStep
from openai.types.beta import AssistantStreamEvent
from functions import getUltraSrtFcst

__INSTRUCTIONS__ = """
당신은 어르신을 돕는 시니어 도우미입니다. 
당신의 이름은 '애비'입니다. 
답변은 짧게 구성을 하며, 어르신을 대할 때는 친근하고 따뜻한 말투를 사용해야합니다.
복잡한 정보는 간단하게 풀어 설명하고, 쉬운 단어를 사용하여 어르신이 편하게 이해할 수 있도록 돕습니다.
어르신이 이전 대화를 기억하지 못할 때, 간단하게 요약해서 설명해 주세요. 예를 들어, '조금 전에 말씀하셨던 내용은 ~였습니다.'와 같은 방식으로 대화를 요약하세요. 
사용자는 대화 모드를 수행중입니다. 특수문자를 사용하지말고 대화형식으로 답변하세요
"""

router = APIRouter()
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")
client = OpenAI()

def override(method: Any) -> Any:
    return method
### 스레드 관리 API ###
#
#    ooooooooooooo oooo                                           .o8 
#    8'   888   `8 `888                                          "888 
#         888       888 .oo.   oooo d8b  .ooooo.   .oooo.    .oooo888 
#         888       888P"Y88b  `888""8P d88' `88b `P  )88b  d88' `888 
#         888       888   888   888     888ooo888  .oP"888  888   888 
#         888       888   888   888     888    .o d8(  888  888   888 
#        o888o     o888o o888o d888b    `Y8bod8P' `Y888""8o `Y8bod88P"
# run state : created, running, processing, waiting, done

# 스레드 생성
async def create_assistant_thread(user_id: int, db: Session = Depends(get_db)):
    thread = client.beta.threads.create()
    
    assistant_thread = AssistantThread(
        user_id=user_id,
        thread_id=thread.id,
        created_at=datetime.utcnow(),
        run_state="created",
        run_id=thread.run_id
    )
    
    db.add(assistant_thread)
    db.commit()
    db.refresh(assistant_thread)
    
    return assistant_thread

# 특정 사용자의 스레드 조회
# id 말고 엑세스토큰으로 찾아야하지 않을까?
# 
@router.get("/threads/{user_id}")
async def get_threads_by_user(user_id: int, db: Session = Depends(get_db)):
    threads = db.query(AssistantThread).filter(AssistantThread.user_id == user_id).all()
    if not threads:
        raise HTTPException(status_code=404, detail="No threads found for this user")
    return threads

# 스레드 삭제
@router.delete("/threads/{user_id}")
async def delete_assistant_thread(user_id: int, db: Session = Depends(get_db)):
    # user_id로 해당 유저의 스레드를 찾음
    thread = db.query(AssistantThread).filter(AssistantThread.user_id == user_id).first()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    
    db.delete(thread)
    db.commit()
    return {"message": "Thread deleted successfully"}

#    ooo        ooooo                                                           
#    `88.       .888'                                                           
#     888b     d'888   .ooooo.   .oooo.o  .oooo.o  .oooo.    .oooooooo  .ooooo. 
#     8 Y88. .P  888  d88' `88b d88(  "8 d88(  "8 `P  )88b  888' `88b  d88' `88b
#     8  `888'   888  888ooo888 `"Y88b.  `"Y88b.   .oP"888  888   888  888ooo888
#     8    Y     888  888    .o o.  )88b o.  )88b d8(  888  `88bod8P'  888    .o
#    o8o        o888o `Y8bod8P' 8""888P' 8""888P' `Y888""8o `8oooooo.  `Y8bod8P'
#                                                           d"     YD           
#                                                           "Y88888P'           
# user id 말고 엑세스토큰으로 찾아야하지 않을까?
@router.post("/message/{user_id}", response_model=AssistantMessageCreate)
async def add_and_run_message(user_id: int, message: AssistantMessageCreate, db: Session = Depends(get_db)):

    thread = db.query(AssistantThread).filter(AssistantThread.user_id == user_id).first()
    if not thread:
        thread = create_assistant_thread(user_id, db)

    running_states = ["running", "processing", "waiting"]
    latest_message = db.query(AssistantMessage).filter(AssistantMessage.thread_id == thread.thread_id).order_by(AssistantMessage.created_at.desc()).first()

    if latest_message and latest_message.status_type in running_states:
        raise HTTPException(status_code=400, detail="A message is already in progress")

    response = client.beta.threads.messages.create(
        thread_id=thread.thread_id,
        role="user",
        content=message.content
    )

    new_message = AssistantMessage(
        thread_id=thread.thread_id,
        sender_type="user",
        status_type="sent",
        content=message.content,
        created_at=datetime.utcnow()
    )
    
    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    async with client.beta.threads.runs.stream(
        thread_id=thread.thread_id,
        instructions=__INSTRUCTIONS__,
        event_handler=EventHandler(db, thread.thread_id, new_message.message_id),
    ) as stream:
        stream.until_done()

    return {"status": "Message created and executed", "message": new_message.content}

# 특정 스레드의 메시지 조회
@router.get("/messages/{user_id}")
async def get_messages_by_thread(user_id: int, db: Session = Depends(get_db)):
    thread = db.query(AssistantThread).filter(AssistantThread.user_id == user_id).first()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    messages = db.query(AssistantMessage).filter(AssistantMessage.thread_id == thread.thread_id).all()
    if not messages:
        raise HTTPException(status_code=404, detail="No messages found for this thread")
    
    return messages


#       .oooooo.                                               .o.       ooooo
#      d8P'  `Y8b                                             .888.      `888'
#     888      888 oo.ooooo.   .ooooo.  ooo. .oo.            .8"888.      888 
#     888      888  888' `88b d88' `88b `888P"Y88b          .8' `888.     888 
#     888      888  888   888 888ooo888  888   888         .88ooo8888.    888 
#     `88b    d88'  888   888 888    .o  888   888        .8'     `888.   888 
#      `Y8bood8P'   888bod8P' `Y8bod8P' o888o o888o      o88o     o8888o o888o
#                   888                                                       
#                  o888o                                                 

class EventHandler(AssistantEventHandler):
    def __init__(self, db: Session, thread_id: str, message_id: str):
        self.db = db
        self.thread_id = thread_id
        self.message_id = message_id
        self.current_run = None

    def update_message_status(self):
        message = self.db.query(AssistantMessage).filter(AssistantMessage.message_id == self.message_id).first()
        if message:
            message.status = self.status
            self.db.commit()
            print(f"Message {self.message_id} status updated to {self.status}")

    def on_event(self, event: Any) -> None:
        if event.event == 'thread.run.requires_action':
            run_id = event.data.id
            self.update_message_status()
            self.handle_requires_action(event.data, run_id)

    @override
    def on_run_created(self, run):
        self.current_run = run

    @override
    def on_error(self, error: Any) -> None:
        print(f'Error: {error}')

    @override
    def on_tool_call_created(self, tool_call):
        self.function_name = tool_call.function.name
        self.tool_id = tool_call.id


    @override
    def handle_requires_action(self, data, run_id):
        tool_outputs = []

        for tool in data.required_action.submit_tool_outputs.tool_calls:
            if tool.function.name == "getUltraSrtFcst":
                result = getUltraSrtFcst()
            if isinstance(result, dict):
                result = json.dumps(result, ensure_ascii=False)
            elif not isinstance(result, str):
                result = str(result)    
            tool_outputs.append({"tool_call_id" : tool.id, "output": result})
        self.submit_tool_outputs(tool_outputs)

    @override
    def submit_tool_outputs(self, tool_outputs):
      with client.beta.threads.runs.submit_tool_outputs_stream(
        thread_id=self.current_run.thread_id,
        run_id=self.current_run.id,
        tool_outputs=tool_outputs,
        event_handler=EventHandler(),
      ) as stream:
        for text in stream.text_deltas:
          print(text, end="", flush=True)
        print()

    @override
    def on_message_done(self, message: Message) -> None:
        print(message.content[0].text.value)
        # db에 업데이트 할것