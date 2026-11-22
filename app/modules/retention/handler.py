from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.dependencies import Service
from app.modules.retention.service import RetentionService

router = APIRouter(prefix="/api/retention")


class CarryOverRequest(BaseModel):
    season: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_一-鿿-]+$")


def retention_service(service: Service) -> RetentionService:
    return RetentionService(service.store)


@router.post("/carry-over", status_code=201)
def carry_over(body: CarryOverRequest, service: Service, response: Response):
    result = retention_service(service).carry_over(body.season)
    if result["reused"]:
        response.status_code = 200
    return result


@router.get("/batches")
def batches(service: Service):
    return retention_service(service).batches()


@router.get("/batches/{batch_id}")
def batch(batch_id: str, service: Service):
    return retention_service(service).batch(batch_id)


@router.get("/runs/{run_id}/conclusion")
def run_conclusion(run_id: str, service: Service):
    # 实时复算单班次结论，不落库；用于结转前预览
    from app.modules.retention.decision import decide
    archive = service.store.fetch(run_id)
    conclusion, _, _ = decide(archive)
    return conclusion
