"""Worker status endpoint – for monitoring active threads."""
from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Worker
from ..schemas import WorkerOut

router = APIRouter(prefix="/api/workers", tags=["workers"])


@router.get("/", response_model=List[WorkerOut])
def list_workers(db: Session = Depends(get_db)):
    return db.query(Worker).all()
