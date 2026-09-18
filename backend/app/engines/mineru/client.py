"""MinerU 在线 API 客户端（tasklist 6.1，TECH_SPEC §8.0）。

唯一允许封装 MinerU 协议的地方：提交批量任务 → PUT 原件 → 轮询 → 下载 ZIP → 抽 `full.md`。
调用方只有 Celery 任务（`engines/celery/ingest.py`）：**API 请求线程不得触发解析**。

约定：

- `.md / .markdown / .txt` 已是文本，按 UTF-8 读取，**不调用 MinerU**；`.pdf / .docx` 才走 API。
- 鉴权 `Authorization: Bearer $MINERU_API_KEY`；Key 只来自配置，**不写日志、不进异常信息**。
- 轮询间隔与单任务上限取配置（默认 5s / 900s），超时按失败处理。
- 批量中任一文件失败即整体失败：批量导入是「一文件一任务」，不存在部分成功语义。
- ZIP 只**在内存中**读取 `full.md`，不落盘解压：外部服务返回的压缩包不能信任其路径
  （避免 zip slip），落盘只保存原始 ZIP（供排查、上传对象存储 `parsed/` 前缀与解出图片）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.common.config import settings

logger = logging.getLogger(__name__)

# 直读为文本、不调用 MinerU 的扩展名
TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
# 允许导入的全部扩展名（tasklist 6.4 白名单）
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}

# 单次 HTTP 请求超时；整体任务上限由 `mineru_task_timeout_seconds` 控制
REQUEST_TIMEOUT_SECONDS = 120.0


class MinerUError(RuntimeError):
    """MinerU 链路失败。message 面向日志与 `parse_error`，不含密钥与响应正文。"""


@dataclass
class ParsedDocument:
    """一个原件的解析结果。

    `zip_path` 是本地保存的原始 ZIP（含 `full.md` 与 `images/`），解析链路之外还要用它
    上传对象存储、解出图片；保存失败或文本直读时为 `None`。
    """

    markdown: str
    zip_path: Path | None = None


class MinerUClient:
    """MinerU 在线 API 客户端。

    用法：`await MinerUClient().parse_batch([Path("a.pdf")])`。
    `transport` 仅用于测试注入（httpx.MockTransport），生产环境不要传。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model_version: str | None = None,
        language: str | None = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
        output_dir: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.mineru_api_base_url).rstrip("/")
        self.api_key = settings.mineru_api_key if api_key is None else api_key
        self.model_version = model_version or settings.mineru_model_version
        self.language = language or settings.mineru_language
        self.poll_interval = (
            settings.mineru_poll_interval_seconds if poll_interval is None else poll_interval
        )
        self.timeout = (
            settings.mineru_task_timeout_seconds if timeout is None else timeout
        )
        self.output_dir = Path(output_dir or settings.mineru_output_dir)
        self._transport = transport

    # --- 对外接口 ---------------------------------------------------------

    async def parse_batch(self, paths: list[Path]) -> dict[str, str]:
        """解析一批文件，返回 `{str(path): markdown}`（只要正文的便捷入口）。"""

        documents = await self.parse_documents(paths)
        return {key: document.markdown for key, document in documents.items()}

    async def parse_documents(self, paths: list[Path]) -> dict[str, ParsedDocument]:
        """解析一批文件，返回 `{str(path): ParsedDocument}`。

        文本文件本地直读（`zip_path=None`）；`pdf/docx` 走 MinerU 并保存原始 ZIP。
        任一文件失败抛 `MinerUError`。
        """

        results: dict[str, ParsedDocument] = {}
        remote: list[Path] = []
        for path in paths:
            if path.suffix.lower() in TEXT_EXTENSIONS:
                results[str(path)] = ParsedDocument(markdown=read_text_file(path))
            else:
                remote.append(path)

        if remote:
            results.update(await self._parse_remote(remote))
        return results

    # --- MinerU 链路 ------------------------------------------------------

    @asynccontextmanager
    async def _client_scope(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._transport is not None:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=REQUEST_TIMEOUT_SECONDS
            ) as client:
                yield client
            return
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            yield client

    async def _parse_remote(self, paths: list[Path]) -> dict[str, ParsedDocument]:
        if not self.api_key:
            raise MinerUError("未配置 MINERU_API_KEY，无法调用 MinerU 解析 PDF/DOCX")

        async with self._client_scope() as client:
            batch_id, upload_urls = await self._submit(client, paths)
            logger.info("MinerU 批量任务已提交：batch=%s files=%s", batch_id, [p.name for p in paths])

            await asyncio.gather(
                *(
                    self._upload(client, url, path)
                    for url, path in zip(upload_urls, paths, strict=True)
                )
            )

            items = await self._poll(client, batch_id)
            results: dict[str, ParsedDocument] = {}
            for index, path in enumerate(paths):
                item = items.get(str(index)) or items.get(path.name)
                if item is None:
                    raise MinerUError(f"MinerU 结果缺少文件：{path.name}（batch={batch_id}）")
                results[str(path)] = await self._fetch_document(client, item, path)
            return results

    async def _submit(
        self, client: httpx.AsyncClient, paths: list[Path]
    ) -> tuple[str, list[str]]:
        """提交批量任务，返回 batch_id 与每个文件对应的 presigned 上传地址。"""

        body = {
            "files": [
                {"name": path.name, "data_id": str(index)}
                for index, path in enumerate(paths)
            ],
            "model_version": self.model_version,
            "language": self.language,
        }
        data = await self._request(
            client, "POST", f"{self.base_url}/file-urls/batch", json=body, authorized=True
        )

        batch_id = data.get("batch_id")
        upload_urls = data.get("file_urls") or []
        if not batch_id:
            raise MinerUError("MinerU 未返回 batch_id")
        if len(upload_urls) != len(paths):
            raise MinerUError(
                f"MinerU 上传地址数量不匹配：期望 {len(paths)}，实际 {len(upload_urls)}"
            )
        return str(batch_id), [str(url) for url in upload_urls]

    async def _upload(self, client: httpx.AsyncClient, url: str, path: Path) -> None:
        """原件直传 presigned 地址（不经业务后端中转，也不带鉴权头）。"""

        content = await asyncio.to_thread(path.read_bytes)
        try:
            response = await client.put(url, content=content)
        except httpx.HTTPError as exc:
            raise MinerUError(
                f"上传原件失败：{path.name}（{type(exc).__name__}）"
            ) from exc
        if response.status_code >= 400:
            raise MinerUError(f"上传原件失败：{path.name} HTTP {response.status_code}")

    async def _poll(
        self, client: httpx.AsyncClient, batch_id: str
    ) -> dict[str, dict]:
        """轮询直到全部终态；返回 {data_id 或 file_name: 结果项}。"""

        deadline = time.monotonic() + self.timeout
        while True:
            data = await self._request(
                client,
                "GET",
                f"{self.base_url}/extract-results/batch/{batch_id}",
                authorized=True,
            )
            items = data.get("extract_result") or []
            states = {item.get("state") for item in items}

            if items and states <= {"done", "failed"}:
                failed = [item for item in items if item.get("state") == "failed"]
                if failed:
                    first = failed[0]
                    raise MinerUError(
                        f"MinerU 解析失败：{first.get('file_name')} {first.get('err_msg') or ''}".strip()
                    )
                keyed: dict[str, dict] = {}
                for item in items:
                    for key in (item.get("data_id"), item.get("file_name")):
                        if key:
                            keyed[str(key)] = item
                return keyed

            if time.monotonic() >= deadline:
                raise MinerUError(
                    f"MinerU 解析超时（超过 {self.timeout}s，batch={batch_id}）"
                )
            await asyncio.sleep(self.poll_interval)

    async def _fetch_document(
        self, client: httpx.AsyncClient, item: dict, path: Path
    ) -> ParsedDocument:
        zip_url = item.get("full_zip_url")
        if not zip_url:
            raise MinerUError(f"MinerU 未返回解析结果地址：{path.name}")

        content = await self._request_bytes(client, str(zip_url))
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                markdown = extract_full_markdown(archive, path.name)
        except zipfile.BadZipFile as exc:
            raise MinerUError(f"解析结果不是有效 ZIP：{path.name}") from exc

        zip_path = await asyncio.to_thread(self._save_zip, content, path.stem)
        return ParsedDocument(markdown=markdown, zip_path=zip_path)

    def _save_zip(self, content: bytes, stem: str) -> Path | None:
        """保存原始 ZIP 供排查与后续上传；失败不影响解析结果，返回 None。"""

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            target = self.output_dir / f"{stem}.zip"
            target.write_bytes(content)
        except OSError:
            logger.warning("保存 MinerU 结果 ZIP 失败：%s", stem, exc_info=True)
            return None
        return target

    # --- HTTP 细节 --------------------------------------------------------

    def _headers(self, *, authorized: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        authorized: bool,
    ) -> dict:
        """发请求并校验 MinerU 的统一信封 `{code, msg, data}`。"""

        try:
            response = await client.request(
                method, url, json=json, headers=self._headers(authorized=authorized)
            )
        except httpx.HTTPError as exc:
            raise MinerUError(f"MinerU 请求失败：{method} {url}（{type(exc).__name__}）") from exc

        if response.status_code >= 400:
            # 只报状态码，不回显响应正文：可能带内部地址或额度信息
            raise MinerUError(f"MinerU 返回 HTTP {response.status_code}：{method} {url}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MinerUError(f"MinerU 返回非 JSON 响应：{method} {url}") from exc

        code = payload.get("code", 0)
        if code != 0:
            raise MinerUError(f"MinerU 业务错误 code={code} msg={payload.get('msg')}")
        return payload.get("data") or {}

    async def _request_bytes(self, client: httpx.AsyncClient, url: str) -> bytes:
        """下载 presigned 结果（不需要鉴权头）。"""

        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise MinerUError(f"下载解析结果失败（{type(exc).__name__}）") from exc
        if response.status_code >= 400:
            raise MinerUError(f"下载解析结果失败 HTTP {response.status_code}")
        return response.content


def read_text_file(path: Path) -> str:
    """按 UTF-8 读取文本类文件（md/markdown/txt），不调用 MinerU。"""

    try:
        # utf-8-sig：Windows 记事本常见 BOM，按 utf-8 解会把 BOM 留在正文里
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MinerUError(f"文本文件不是 UTF-8 编码：{path.name}") from exc


def extract_full_markdown(archive: zipfile.ZipFile, name: str) -> str:
    """从 MinerU 结果 ZIP 中取出 `full.md`（找不到则退而取第一个 .md）。"""

    entries = archive.namelist()
    candidates = [entry for entry in entries if entry.endswith("full.md")] or [
        entry for entry in entries if entry.endswith(".md")
    ]
    if not candidates:
        raise MinerUError(f"结果 ZIP 内没有 full.md：{name}")
    return archive.read(candidates[0]).decode("utf-8", errors="replace")
