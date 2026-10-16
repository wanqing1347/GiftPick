# -*- coding: utf-8 -*-
"""fetch_webpage_tool

网页正文抓取工具（HTTP GET + HTML 去标签）：ResearchAgent 调研时读取来源正文。
web_search_tool 只返回搜索摘要片段；要判断"口碑到底怎么样"必须抓原文。

设计约束：
    - 只读：GET 请求，不做任何写入；
    - 仅支持 text/html / text/plain（JSON 也放过）——图片、视频、PDF 不抓；
    - 正文有截断上限，超限标 truncated=true，避免长网页冲垮子代理上下文；
    - 不执行 JavaScript、不保存 Cookie：动态渲染站点可能抓不全，属已知局限，
      工具结果里如实提示，不伪装成"已读完"。

注意：本模块不能用 `from __future__ import annotations`（AgentScope schema 生成依赖运行时注解）。
"""
import json
from html.parser import HTMLParser

import httpx
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus

_FETCH_TIMEOUT = 15.0
_USER_AGENT = "Mozilla/5.0 (compatible; GiftPickResearchAgent/1.0)"


class _TextExtractor(HTMLParser):
    """极简 HTML→文本：丢掉 script/style 与标签，保留按块级换行的可见文本。"""

    _SKIP = {"script", "style", "noscript", "template", "svg"}
    _BLOCK_END = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                  "section", "article", "blockquote", "br", "ul", "ol", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._capture_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if self._skip_depth == 0 and tag == "title":
            self._capture_title = True
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK_END and self._skip_depth == 0:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._capture_title = False
        elif tag in self._BLOCK_END and self._skip_depth == 0:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._capture_title:
            self.title += data.strip()
        elif self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        """合并空白：多行压成单空格之间保留段落换行。"""
        lines = []
        for line in "".join(self._chunks).splitlines():
            collapsed = " ".join(line.split())
            if collapsed:
                lines.append(collapsed)
        return "\n".join(lines)


def html_to_text(html: str) -> tuple[str, str]:
    """HTML 抽正文，返回 (正文, <title>)。解析失败返回 (原文清洗, 空标题)。"""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
        return extractor.text(), extractor.title
    except Exception:  # noqa: BLE001 —— 极端畸形 HTML 不能让整次抓取挂掉
        return "\n".join(line.strip() for line in html.splitlines() if line.strip()), ""


def build_webpage_fetch_tool(bus: TradeEventBus, max_chars: int = 6000, client=None):
    """构造抓正文工具；client 参数供测试注入 httpx.MockTransport。"""

    async def fetch_webpage_tool(url: str) -> ToolChunk:
        """抓取网页正文并抽取为纯文本，用于调研外部口碑、评测与指南。仅限 GET 读取。

        Args:
            url (`str`):
                要抓取的网页地址（http/https），通常来自 web_search_tool 返回的 url。
        """
        session_id = ShoppingContext.current_session_id()
        bus.publish(session_id, "tool.invoke", {"tool": "fetch_webpage_tool", "args": {"url": url}})
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolChunk(
                content=[TextBlock(text="[error] 仅支持 http/https URL")],
                state=ToolResultState.ERROR,
            )
        owned = client is None
        try:
            client0 = client or httpx.AsyncClient(
                timeout=_FETCH_TIMEOUT, follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            )
            try:
                response = await client0.get(url)
            finally:
                if owned:
                    await client0.aclose()
        except (httpx.HTTPError, ValueError) as err:
            bus.publish(session_id, "tool.result", {"tool": "fetch_webpage_tool", "error": str(err)})
            return ToolChunk(
                content=[TextBlock(text=f"[error] 网页抓取失败：{err}")],
                state=ToolResultState.ERROR,
            )
        if response.status_code >= 400:
            return ToolChunk(
                content=[TextBlock(text=f"[error] 目标网页返回 HTTP {response.status_code}，未读到正文")],
                state=ToolResultState.ERROR,
            )
        # 二进制资源（图片/PDF）没有可引用的正文，如实拒绝而不是把乱码当结论。
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in ("text/html", "text/plain", "application/json"):
            return ToolChunk(
                content=[TextBlock(text=f"[error] 不支持抓取 {content_type or '未知类型'} 的内容，请换文字类来源")],
                state=ToolResultState.ERROR,
            )
        raw = response.text
        text, title = html_to_text(raw) if content_type == "text/html" else (raw.strip(), "")
        truncated = len(text) > max_chars
        payload = {
            "url": str(response.url),
            "title": title,
            "content_type": content_type,
            "text": text[:max_chars],
            "truncated": truncated,
        }
        if truncated:
            payload["notice"] = "正文被截断，结论可能漏掉后文；需要更多内容请换来源或用 query 定位具体段落。"
        bus.publish(session_id, "tool.result", {
            "tool": "fetch_webpage_tool", "chars": len(payload["text"]),
            "truncated": truncated, "status_code": response.status_code,
        })
        return ToolChunk(
            content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    return fetch_webpage_tool
