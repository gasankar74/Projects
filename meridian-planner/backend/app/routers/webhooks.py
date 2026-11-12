"""Inbound Google push notifications. Public endpoint: authenticated by the per-channel secret token."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..services.push import handle_notification

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/google", include_in_schema=False)
def google_push(request: Request, db: Session = Depends(get_db)):
    h = request.headers
    handle_notification(db, h.get("x-goog-channel-id"), h.get("x-goog-channel-token"), h.get("x-goog-resource-state"))
    # Always 200: Google retries other codes, and the response must not reveal which channels exist.
    return Response(status_code=200)
