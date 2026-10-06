"""Image upload boundaries for the library and session attachments."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from deepsearch_agent.document_api import local_origin, writable
from deepsearch_agent.documents import DocumentError
from deepsearch_agent.image_assets import IMAGE_TYPES, MAX_IMAGE_BYTES

router = APIRouter(tags=['图片'])


async def read_image_upload(request: Request, filename: str):
    local_origin(request)
    if len(filename) > 512 or not filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
        raise HTTPException(415, '仅支持 PNG、JPEG、WebP 图片')
    if request.headers.get('content-type', '').split(';')[0].strip() not in IMAGE_TYPES:
        raise HTTPException(415, '请以正确的图片类型上传')
    try:
        declared = int(request.headers.get('content-length', '0'))
    except ValueError:
        raise HTTPException(400, '上传长度无效') from None
    if declared > MAX_IMAGE_BYTES:
        raise HTTPException(413, '图片不得超过 10 MiB')
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
            raise HTTPException(413, '图片不得超过 10 MiB')
        data.extend(chunk)
    return bytes(data)


def require_session(request: Request, session_id: str):
    try:
        request.app.state.conversations.get(session_id)
    except KeyError:
        raise HTTPException(404, '会话不存在') from None


@router.post('/api/documents/upload-image', status_code=202)
async def upload_library_image(request: Request, filename: str = 'image.png'):
    writable(request)
    content = await read_image_upload(request, filename)
    try:
        return await run_in_threadpool(request.app.state.documents.submit_image, content, filename)
    except DocumentError as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(500, '图片保存失败，请检查磁盘空间和权限') from None


@router.post('/api/sessions/{session_id}/attachments', status_code=201)
async def upload_attachment(session_id: str, request: Request, filename: str = 'image.png'):
    require_session(request, session_id)
    if not request.app.state.vision_enabled:
        raise HTTPException(409, '请先配置视觉模型后带图提问')
    content = await read_image_upload(request, filename)
    try:
        return await run_in_threadpool(request.app.state.attachments.save, session_id, content, filename)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except OSError:
        raise HTTPException(500, '图片附件保存失败') from None


@router.get('/api/sessions/{session_id}/attachments/{ident}/image')
def attachment_image(session_id: str, ident: str, request: Request):
    require_session(request, session_id)
    try:
        path = request.app.state.attachments.image(session_id, ident)
        return FileResponse(path, media_type='image/png', headers={'X-Content-Type-Options': 'nosniff'})
    except KeyError:
        raise HTTPException(404, '图片附件不存在或不属于当前会话') from None


@router.post('/api/sessions/{session_id}/attachments/{ident}/index', status_code=202)
def index_attachment(session_id: str, ident: str, request: Request):
    writable(request)
    require_session(request, session_id)
    try:
        store = request.app.state.attachments
        record = store.get(session_id, ident)
        return request.app.state.documents.submit_image(store.image(session_id, ident).read_bytes(), record['title'])
    except KeyError:
        raise HTTPException(404, '图片附件不存在或不属于当前会话') from None
    except DocumentError as error:
        raise HTTPException(409, str(error)) from None
    except OSError:
        raise HTTPException(500, '图片入库失败，请检查磁盘') from None
