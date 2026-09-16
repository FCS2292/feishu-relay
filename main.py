"""
飞书(Lark) Open API 中转服务
----------------------------------
用途:把飞书自建应用的 App ID / App Secret 保存在你自己的托管环境变量里,
由本服务负责换取并缓存 tenant_access_token,再把请求转发给飞书开放平台。
对外只暴露一个用 Bearer API Key 保护的简单接口,方便 Perplexity Computer
通过 custom-credentials(Bearer 类型)直接调用,无需把飞书密钥暴露给任何第三方。

需要设置的环境变量:
  FEISHU_APP_ID      飞书自建应用的 App ID
  FEISHU_APP_SECRET  飞书自建应用的 App Secret
  API_KEY            你自己生成的一串随机字符串,作为本服务对外的访问密钥
                      (这个值之后会作为 Bearer Token 注册进 Perplexity 的 custom-credentials)

本地测试:
  pip install -r requirements.txt
  FEISHU_APP_ID=xxx FEISHU_APP_SECRET=xxx API_KEY=xxx uvicorn main:app --reload
"""

import asyncio
import os
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse, Response

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
API_KEY = os.environ.get("API_KEY")

FEISHU_BASE = "https://open.feishu.cn"
TOKEN_URL = f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal"

# 只放行只读的云文档/云盘/知识库/表格/多维表格相关接口前缀,避免误开放写权限
ALLOWED_PREFIXES = (
    "drive/v1/",       # 云盘:文件夹、文件清单、文件元数据
    "docx/v1/",        # 文档:纯文本内容、块内容
    "wiki/v2/",        # 知识库:空间、节点
    "sheets/v3/",       # 电子表格
    "bitable/v1/",      # 多维表格
)

app = FastAPI(title="Feishu Relay for Perplexity Computer")

_token_cache = {"value": None, "expires_at": 0.0}


def _startup_check():
    missing = [
        name
        for name, val in [
            ("FEISHU_APP_ID", FEISHU_APP_ID),
            ("FEISHU_APP_SECRET", FEISHU_APP_SECRET),
            ("API_KEY", API_KEY),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(f"缺少必要的环境变量: {', '.join(missing)}")


_startup_check()


async def get_tenant_access_token() -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["value"]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            TOKEN_URL,
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    data = resp.json()
    if data.get("code") != 0:
        raise HTTPException(status_code=502, detail=f"获取 tenant_access_token 失败: {data}")

    _token_cache["value"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200)
    return _token_cache["value"]


def check_auth(authorization: Optional[str]):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 鉴权")
    token = authorization.split(" ", 1)[1].strip()
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="API Key 无效")


@app.get("/health")
async def health():
    return {"ok": True}


async def _feishu_get(path: str, params: dict):
    token = await get_tenant_access_token()
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{FEISHU_BASE}/open-apis/{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    return resp.status_code, resp.json()


@app.get("/files")
async def list_files(
    folder_token: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: int = 50,
    authorization: Optional[str] = Header(None),
):
    """列出云盘某文件夹下的文件/子文件夹。不传 folder_token 则列出应用可见的根目录。"""
    check_auth(authorization)
    params = {"page_size": page_size}
    if folder_token:
        params["folder_token"] = folder_token
    if page_token:
        params["page_token"] = page_token
    status, data = await _feishu_get("drive/v1/files", params)
    return JSONResponse(status_code=status, content=data)


@app.get("/document/{document_id}/content")
async def get_doc_content(document_id: str, authorization: Optional[str] = Header(None)):
    """获取一篇云文档(docx)的纯文本内容。"""
    check_auth(authorization)
    status, data = await _feishu_get(f"docx/v1/documents/{document_id}/raw_content", {})
    return JSONResponse(status_code=status, content=data)


@app.get("/wiki/spaces")
async def list_wiki_spaces(
    page_token: Optional[str] = None,
    page_size: int = 20,
    authorization: Optional[str] = Header(None),
):
    """列出应用可访问的知识库空间。"""
    check_auth(authorization)
    params = {"page_size": page_size}
    if page_token:
        params["page_token"] = page_token
    status, data = await _feishu_get("wiki/v2/spaces", params)
    return JSONResponse(status_code=status, content=data)


@app.get("/wiki/spaces/{space_id}/nodes")
async def list_wiki_nodes(
    space_id: str,
    parent_node_token: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: int = 20,
    authorization: Optional[str] = Header(None),
):
    """列出某知识库空间下(某父节点下)的子节点,可据此拿到文档/表格的 token。"""
    check_auth(authorization)
    params = {"page_size": page_size}
    if parent_node_token:
        params["parent_node_token"] = parent_node_token
    if page_token:
        params["page_token"] = page_token
    status, data = await _feishu_get(f"wiki/v2/spaces/{space_id}/nodes", params)
    return JSONResponse(status_code=status, content=data)


@app.get("/export/full")
async def export_full(
    token: str,
    type: str,
    file_extension: str,
    sub_id: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    """
    一次性完成:创建导出任务 -> 轮询直到完成 -> 下载文件,直接把二进制文件流返回。
    用法示例: /export/full?token=xxx&type=slides&file_extension=pptx
    支持的 type/file_extension 组合以飞书官方文档为准(docx/doc->docx或pdf, sheet->xlsx或csv, bitable->xlsx或csv)。
    幻灯片(slides)类型是否被飞书支持导出,需要实际调用验证,飞书官方文档未明确列出。
    """
    check_auth(authorization)
    access_token = await get_tenant_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    payload = {"file_extension": file_extension, "token": token, "type": type}
    if sub_id:
        payload["sub_id"] = sub_id

    async with httpx.AsyncClient(timeout=30) as client:
        create_resp = await client.post(
            f"{FEISHU_BASE}/open-apis/drive/v1/export_tasks",
            json=payload,
            headers=headers,
        )
    create_data = create_resp.json()
    if create_data.get("code") != 0:
        return JSONResponse(status_code=create_resp.status_code, content={"stage": "create", **create_data})

    ticket = create_data["data"]["ticket"]

    file_token = None
    last_status = None
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(15):
            await asyncio.sleep(2)
            status_resp = await client.get(
                f"{FEISHU_BASE}/open-apis/drive/v1/export_tasks/{ticket}",
                params={"token": token},
                headers=headers,
            )
            status_data = status_resp.json()
            if status_data.get("code") != 0:
                return JSONResponse(status_code=status_resp.status_code, content={"stage": "poll", **status_data})
            last_status = status_data
            result = status_data.get("data", {}).get("result", {})
            job_status = result.get("job_status")
            if job_status == 0:
                file_token = result.get("file_token")
                break
            if job_status not in (1, 2, None):
                return JSONResponse(status_code=400, content={"stage": "poll_failed", **status_data})

    if not file_token:
        return JSONResponse(status_code=504, content={"stage": "timeout", "last_status": last_status})

    async with httpx.AsyncClient(timeout=60) as client:
        dl_resp = await client.get(
            f"{FEISHU_BASE}/open-apis/drive/v1/export_tasks/file/{file_token}/download",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if dl_resp.status_code != 200:
        return JSONResponse(status_code=dl_resp.status_code, content={"stage": "download", "detail": dl_resp.text[:500]})

    media_type = dl_resp.headers.get("content-type", "application/octet-stream")
    return Response(content=dl_resp.content, media_type=media_type)


@app.get("/file/download")
async def download_raw_file(token: str, authorization: Optional[str] = Header(None)):
    """
    直接下载云盘里类型为 file 的原始文件(已上传的 PDF/PPTX/DOCX 等),无需经过导出任务。
    用法示例: /file/download?token=xxx
    适用于 drive/v1/files 列出结果中 type="file" 的条目(type=docx/sheet/slides 请用对应的专用接口或 /export/full)。
    """
    check_auth(authorization)
    access_token = await get_tenant_access_token()
    async with httpx.AsyncClient(timeout=60) as client:
        dl_resp = await client.get(
            f"{FEISHU_BASE}/open-apis/drive/v1/files/{token}/download",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if dl_resp.status_code != 200:
        return JSONResponse(status_code=dl_resp.status_code, content={"stage": "download", "detail": dl_resp.text[:500]})

    media_type = dl_resp.headers.get("content-type", "application/octet-stream")
    return Response(content=dl_resp.content, media_type=media_type)


@app.get("/feishu/{feishu_path:path}")
async def proxy_get(feishu_path: str, request: Request, authorization: Optional[str] = Header(None)):
    check_auth(authorization)

    if not any(feishu_path.startswith(p) for p in ALLOWED_PREFIXES):
        raise HTTPException(status_code=403, detail=f"路径未在白名单内: {feishu_path}")

    token = await get_tenant_access_token()
    url = f"{FEISHU_BASE}/open-apis/{feishu_path}"

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            url,
            params=dict(request.query_params),
            headers={"Authorization": f"Bearer {token}"},
        )

    return JSONResponse(status_code=resp.status_code, content=resp.json())
