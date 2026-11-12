"""The AI assistant: conversations, messages, and confirming proposed changes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import assistant as ai
from ..ai.providers import get_provider
from ..db import get_db
from ..models import AiAction, AiConversation, AiMessage, User
from ..security import current_user

router = APIRouter(prefix="/assistant", tags=["assistant"])

TOOL_LABELS = {
    "find_tasks": "Looked up tasks",
    "get_task": "Read a task",
    "get_agenda": "Read the day's plan",
    "get_risks": "Checked deadline risks",
    "create_task": "Created a task",
    "update_task": "Updated a task",
    "reschedule_task": "Rescheduled a task",
    "bulk_update": "Updated several tasks",
    "complete_task": "Completed a task",
    "delete_task": "Deleted a task",
    "what_if": "Ran a what-if preview",
    "replan": "Replanned",
}


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)


def _own_conv(db: Session, user: User, cid: uuid.UUID) -> AiConversation:
    c = db.get(AiConversation, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    return c


def _msg_out(m: AiMessage) -> dict:
    out = {"id": str(m.id), "seq": m.seq, "role": m.role, "created_at": m.created_at, "changes": m.changes or [], "action_id": str(m.action_id) if m.action_id else None}
    if m.role == "tool":
        # The raw tool result is what the model saw (masked); the app shows a label and the saved changes instead.
        label = TOOL_LABELS.get(m.tool_name or "", m.tool_name or "Tool")
        failed = m.content.startswith('{"error"')
        waiting = '"needs_confirmation": true' in m.content
        out |= {"tool": m.tool_name, "content": label + (" (failed)" if failed else " (waiting for you)" if waiting else "")}
    else:
        out["content"] = m.content
        out["tools"] = [TOOL_LABELS.get(c.get("name"), c.get("name")) for c in (m.tool_calls or [])]
    return out


def _action_out(a: AiAction) -> dict:
    return {"id": str(a.id), "summary": a.summary, "status": a.status, "preview": a.preview, "result": a.result, "created_at": a.created_at, "decided_at": a.decided_at}


def _conv_out(c: AiConversation) -> dict:
    return {"id": str(c.id), "title": c.title, "provider": c.provider, "model": c.model, "created_at": c.created_at, "updated_at": c.updated_at}


@router.get("/status")
def status(user: User = Depends(current_user)):
    p = get_provider()
    h = p.health()
    return {
        "provider": p.name,
        "model": p.model,
        "local": p.local,
        "ok": h.get("ok", False),
        "error": h.get("error"),
        "ai_opt_in": user.ai_opt_in,
    }


@router.get("/conversations")
def list_conversations(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(AiConversation).where(AiConversation.user_id == user.id).order_by(AiConversation.updated_at.desc()).limit(50)).all()
    return [_conv_out(c) for c in rows]


@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationIn | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = AiConversation(user_id=user.id, title=(body.title if body and body.title else "New conversation"), refs={})
    db.add(c)
    db.commit()
    return _conv_out(c)


@router.get("/conversations/{cid}")
def get_conversation(cid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = _own_conv(db, user, cid)
    msgs = db.scalars(select(AiMessage).where(AiMessage.conversation_id == c.id).order_by(AiMessage.seq)).all()
    actions = db.scalars(select(AiAction).where(AiAction.conversation_id == c.id).order_by(AiAction.created_at)).all()
    return {"conversation": _conv_out(c), "messages": [_msg_out(m) for m in msgs], "actions": [_action_out(a) for a in actions]}


@router.delete("/conversations/{cid}", status_code=204)
def delete_conversation(cid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(_own_conv(db, user, cid))
    db.commit()


@router.post("/conversations/{cid}/messages")
def post_message(cid: uuid.UUID, body: MessageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = _own_conv(db, user, cid)
    new = ai.run_turn(db, user, c, body.text.strip())
    actions = db.scalars(select(AiAction).where(AiAction.conversation_id == c.id).order_by(AiAction.created_at)).all()
    return {"conversation": _conv_out(c), "messages": [_msg_out(m) for m in new], "actions": [_action_out(a) for a in actions]}


def _own_action(db: Session, user: User, aid: uuid.UUID) -> AiAction:
    a = db.get(AiAction, aid)
    if not a or a.user_id != user.id:
        raise HTTPException(404, "Proposed change not found")
    return a


@router.post("/actions/{aid}/confirm")
def confirm(aid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = _own_action(db, user, aid)
    conv = db.get(AiConversation, a.conversation_id)
    msg = ai.decide(db, user, conv, a, True)
    return {"message": _msg_out(msg), "action": _action_out(db.get(AiAction, aid))}


@router.post("/actions/{aid}/cancel")
def cancel(aid: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = _own_action(db, user, aid)
    conv = db.get(AiConversation, a.conversation_id)
    msg = ai.decide(db, user, conv, a, False)
    return {"message": _msg_out(msg), "action": _action_out(db.get(AiAction, aid))}
