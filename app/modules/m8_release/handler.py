from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, StringConstraints, conint

from app.modules.m8_release.archiving import submit_release

router = APIRouter(prefix="/api", tags=["撤单释放"])
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class Substitute(BaseModel):
    id: Name
    fabric: Name
    width: conint(strict=True, ge=1, le=4000)
    net: conint(strict=True, ge=1, le=1000000)
    seam: conint(strict=True, ge=0, le=1000000)
    quantity: conint(strict=True, ge=1)


class ReleaseRequest(BaseModel):
    cancel_order: Name
    substitute: Substitute


@router.post("/runs/{run_id}/releases", status_code=201)
def create_release(run_id: str, payload: ReleaseRequest, request: Request, response: Response):
    manifest, reused = submit_release(request.app.state.archive, run_id, payload.cancel_order,
                                      payload.substitute.model_dump())
    if reused:
        response.status_code = 200
    return {"manifest": manifest, "reused": reused}


@router.get("/runs/{run_id}/releases")
def releases(run_id: str, request: Request):
    request.app.state.archive.read(run_id)
    return {"releases": request.app.state.archive.releases(run_id)}


@router.get("/releases/{release_id}")
def release(release_id: str, request: Request):
    return request.app.state.archive.read_release(release_id)
