from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from deepsearch_agent.documents import DocumentError, MAX_UPLOAD_BYTES

router = APIRouter(prefix='/api/documents', tags=['PDF 文献'])


def writable(request):
    origin = request.headers.get('origin')
    if origin and origin not in {'http://127.0.0.1:5173', 'http://localhost:5173',
                                  'http://127.0.0.1:8000', 'http://localhost:8000'}:
        raise HTTPException(403, '仅允许本地工作台上传')
    if request.app.state.demo_retrieval:
        raise HTTPException(409, '请先配置真实 Embedding，再上传 PDF')
    if request.app.state.write_blocked:
        raise HTTPException(409, '知识库写入受保护，请先修复快照')


@router.post('/upload', status_code=202)
async def upload(request: Request, filename: str = 'document.pdf'):
    writable(request)
    if not filename.lower().endswith('.pdf') or len(filename) > 512:
        raise HTTPException(415, '仅支持 PDF 文件')
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/pdf':
        raise HTTPException(415, '请以 application/pdf 上传')
    try:
        declared = int(request.headers.get('content-length', '0'))
    except ValueError:
        raise HTTPException(400, '上传长度无效') from None
    if declared > MAX_UPLOAD_BYTES:
        raise HTTPException(413, 'PDF 不得超过 10 MiB')
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, 'PDF 不得超过 10 MiB')
        data.extend(chunk)
    if not data:
        raise HTTPException(400, '文件为空')
    if not data.startswith(b'%PDF-'):
        raise HTTPException(415, '文件内容不是有效 PDF')
    try:
        return await run_in_threadpool(request.app.state.documents.submit, bytes(data), filename)
    except DocumentError as error:
        raise HTTPException(409, str(error)) from None
    except OSError:
        raise HTTPException(500, '上传保存失败，请检查磁盘空间和权限') from None


@router.get('/{ident}')
def detail(ident: str, request: Request):
    try:
        return request.app.state.documents.view(ident)
    except KeyError:
        raise HTTPException(404, '文献不存在') from None


@router.post('/{ident}/retry', status_code=202)
def retry(ident: str, request: Request):
    writable(request)
    try:
        return request.app.state.documents.retry(ident)
    except KeyError:
        raise HTTPException(404, '文献不存在') from None
    except DocumentError as error:
        raise HTTPException(409, str(error)) from None


@router.get('/{ident}/pages/{number}')
def page(ident: str, number: int, request: Request):
    try:
        return request.app.state.documents.page(ident, number)
    except KeyError:
        raise HTTPException(404, '文献或页码不存在，或尚未解析完成') from None


@router.get('/{ident}/pages/{number}/image')
def image(ident: str, number: int, request: Request):
    try:
        path = request.app.state.documents.image(ident, number)
        return FileResponse(path, media_type='image/png', headers={'X-Content-Type-Options': 'nosniff'})
    except KeyError:
        raise HTTPException(404, '文献或页码不存在') from None
    except DocumentError as error:
        raise HTTPException(422, str(error)) from None


@router.get('/{ident}/file')
def original(ident: str, request: Request):
    try:
        library = request.app.state.documents
        record = library.view(ident)
        path = library.path(ident, '.pdf')
        if not path.is_file():
            raise KeyError()
        return FileResponse(path, media_type='application/pdf', filename=record['title'],
                            headers={'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': 'sandbox'})
    except KeyError:
        raise HTTPException(404, '原始文件不存在') from None
