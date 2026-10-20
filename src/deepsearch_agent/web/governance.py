"""Protected operational views; identities are taken from authenticated state."""
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from deepsearch_agent.core.errors import ApplicationError
from deepsearch_agent.core.governance import GovernanceDenied
from deepsearch_agent.web.document_api import local_origin

router = APIRouter(prefix='/api/governance',tags=['运行治理'])


def service(request):
    return request.app.state.governance


def admin(request, *, write=False):
    user = getattr(request.state,'access_user',None)
    if user is not None and not user.get('is_admin'):
        raise ApplicationError(403,'需要管理员权限')
    if write:
        local_origin(request)
    return user['id'] if user else 'local-operator'


@router.get('/usage')
def usage(request: Request):
    user = getattr(request.state,'access_user',None)
    workspace = getattr(request.state,'workspace_id',None)
    governor = service(request)
    result = {'unit':'internal_cost_units','accounting_day':'UTC',
              'unit_rates':{kind:getattr(governor.settings,kind+'_unit_rate') for kind in ('llm','embedding','image_embedding')},
              'user':governor.store.usage('user',user['id'] if user else 'local',governor.settings.user_daily_units)}
    if workspace is not None or user is None:
        result['workspace'] = governor.store.usage('workspace',workspace or 'local',governor.settings.workspace_daily_units)
    return result


@router.get('/metrics')
def metrics(request: Request):
    admin(request)
    return Response(service(request).render(),media_type='text/plain; version=0.0.4; charset=utf-8',headers={'Cache-Control':'no-store'})


@router.get('/status')
def status(request: Request):
    admin(request)
    return service(request).snapshot()


@router.get('/alerts')
def alerts(request: Request):
    admin(request)
    return service(request).snapshot()['alerts']


class QuotaLimit(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    daily_units: int | None = Field(ge=0,le=10**12)


@router.put('/limits/{scope}/{subject}')
def set_limit(scope: str, subject: str, body: QuotaLimit, request: Request):
    actor = admin(request,write=True)
    try:
        service(request).store.set_limit(scope,subject,body.daily_units,actor)
    except ValueError:
        raise ApplicationError(422,'配额范围或数值无效') from None
    return {'updated':True}


@router.get('/reservations/unknown')
def unknown(request: Request):
    admin(request)
    return service(request).store.unsettled()


@router.get('/audit')
def audit(request: Request):
    admin(request)
    return service(request).store.audit()


class Settlement(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    actual_units: int = Field(ge=0,le=10**12)
    proof_reference: str = Field(min_length=1,max_length=500)
    confirmed: bool


@router.post('/reservations/{ident}/settle')
def settle(ident: str, body: Settlement, request: Request):
    actor = admin(request,write=True)
    if not body.confirmed or not body.proof_reference.strip():
        raise ApplicationError(422,'需要核实费用并填写证明引用')
    service(request).store.settle_unknown(ident,body.actual_units,actor,body.proof_reference.strip())
    return {'settled':True}
