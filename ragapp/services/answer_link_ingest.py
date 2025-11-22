from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from django.conf import settings

# 선택: bs4 있으면 제목/메타 설명을 좀 더 정확하게 뽑음
try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

# robots.txt 준수
from urllib import robotparser

from ragapp.services.utils import extract_urls_from_text
from ragapp.services.ingest import indexto_chroma_safe

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 환경/상수
# ─────────────────────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (compatible; RAGNewsBot/1.0; +https://example.local/bot) "
    "ChatClient/QA-RAG"
)

_CRAWL_ENABLED = bool(getattr(settings, "CRAWL_ANSWER_LINKS", True))
_MAX_LINKS = int(getattr(settings, "ANSWER_LINK_MAX", 5))
_TIMEOUT = int(getattr(settings, "ANSWER_LINK_TIMEOUT", 12))

# 안전 옵션(요약만 저장 / 원문 금지)
_SAFE_SUMMARY_ONLY = bool(getattr(settings, "SAFE_SUMMARY_ONLY", True))
_SAFE_MODE_ENABLED = bool(getattr(settings, "SAFE_MODE_ENABLED", True))

# 로봇/도메인 통제
_RESPECT_ROBOTS = bool(getattr(settings, "RESPECT_ROBOTS", True))
_ALLOWLIST = [d.lower() for d in getattr(settings, "ALLOWLIST_DOMAINS", []) or []]
_RATE_PER_HOST = float(getattr(settings, "CRAWL_RATE_LIMIT_PER_HOST", 1.0))  # e.g. 1 req/sec/host

# 스니펫 길이 제한
_MAX_EXCERPT = int(getattr(settings, "MAX_EXCERPT_CHARS", 0) or 0)  # 0이면 내부 디폴트 사용
_SNIPPET_LEN = _MAX_EXCERPT if _MAX_EXCERPT > 0 else 500

# 호스트별 마지막 요청시각(초간단 레이트리밋)
_last_hit: Dict[str, float] = {}

# robots 캐시
_robots_cache: Dict[str, robotparser.RobotFileParser] = {}


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────
def _domain(u: str) -> str:
    try:
        netloc = urlparse(u).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""

def _is_allowed_domain(u: str) -> bool:
    if not _ALLOWLIST:
        return True  # 화이트리스트 비어있으면 전체 허용(프로덕션에선 채우는 걸 권장)
    d = _domain(u)
    # 서브도메인 포함 허용: endswith 체크
    return any(d == w or d.endswith("." + w) for w in _ALLOWLIST)

def _respect_rate_limit(u: str):
    host = _domain(u)
    if not host or _RATE_PER_HOST <= 0:
        return
    now = time.time()
    last = _last_hit.get(host, 0.0)
    min_interval = 1.0 / _RATE_PER_HOST
    wait = last + min_interval - now
    if wait > 0:
        time.sleep(min(wait, 1.0))
    _last_hit[host] = time.time()

def _robots_ok(u: str) -> bool:
    if not _RESPECT_ROBOTS:
        return True
    try:
        p = urlparse(u)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        rp = _robots_cache.get(robots_url)
        if rp is None:
            # requests로 로드 후 robotparser에 주입(타임아웃 제어)
            try:
                r = requests.get(robots_url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
                txt = r.text if r.status_code == 200 else ""
            except Exception:
                txt = ""
            rp = robotparser.RobotFileParser()
            rp.parse(txt.splitlines())
            _robots_cache[robots_url] = rp
        return rp.can_fetch(_UA, u)
    except Exception:
        # 보수적으로 허용(망가진 robots로 전체 차단되면 UX 나빠짐)
        return True

def _extract_title_and_desc(html: str) -> tuple[str, str]:
    if not html:
        return "", ""
    title, desc = "", ""
    if BeautifulSoup is None:
        # 최소 파싱: <title> 스캔
        try:
            start = html.lower().find("<title>")
            end = html.lower().find("</title>")
            if start != -1 and end != -1 and end > start:
                title = html[start + 7:end].strip()
        except Exception:
            pass
        # 본문 일부를 스니펫으로
        try:
            text = " ".join(html.split())  # 초간단 공백 정리
            desc = text[:_SNIPPET_LEN]
        except Exception:
            desc = ""
        return title, desc

    try:
        soup = BeautifulSoup(html, "html.parser")
        t = soup.find("title")
        if t and t.text:
            title = t.text.strip()
        # 메타 디스크립션 우선
        m = soup.find("meta", attrs={"name": "description"})
        if not m:
            m = soup.find("meta", attrs={"property": "og:description"})
        if m and m.get("content"):
            desc = m.get("content").strip()
        if not desc:
            # 본문 텍스트에서 초간단 스니펫
            body_text = soup.get_text(" ", strip=True)
            desc = (body_text or "")[:_SNIPPET_LEN]
    except Exception:
        # 파싱 실패 시 폴백 없음
        pass
    return title, desc

def _fetch_page(url: str) -> Optional[Dict]:
    if not _is_allowed_domain(url):
        return None
    if not _robots_ok(url):
        return None

    _respect_rate_limit(url)

    try:
        r = requests.get(
            url,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
            timeout=_TIMEOUT,
        )
    except Exception as e:
        log.debug("answer-link fetch error: %s (%s)", url, e)
        return None

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "text/html" not in ctype and "application/xhtml+xml" not in ctype:
        return None

    title, snippet = _extract_title_and_desc(r.text or "")

    # 안전규칙: 항상 메타-전용 저장(본문은 금지)
    news_item = {
        "title": title or url,
        "url": url,
        "source": _domain(url),
        "published_at": "",
        "snippet": (snippet or "")[:_SNIPPET_LEN],
        "news_body": "" if (_SAFE_MODE_ENABLED or _SAFE_SUMMARY_ONLY) else "",  # 강제 빈 본문
    }
    return news_item


# ─────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────
def safe_auto_ingest_answer_links(question: str, answer_text: str) -> Dict:
    """
    답변 안의 URL을 추출해 '메타-전용'으로 인덱싱한다.
    - ALLOWLIST_DOMAINS 준수
    - robots.txt 준수(RESPECT_ROBOTS=True일 때)
    - 본문 저장 금지(요약/메타만 저장)
    - 호스트별 레이트리밋 준수
    """
    if not _CRAWL_ENABLED:
        return {"status": "skip", "reason": "CRAWL_ANSWER_LINKS disabled"}

    try:
        urls: List[str] = extract_urls_from_text(answer_text or "")
        urls = urls[:_MAX_LINKS]
        if not urls:
            return {"status": "skip", "reason": "no urls found in answer"}

        items: List[Dict] = []
        seen = set()
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            item = _fetch_page(u)
            if item:
                items.append(item)

        if not items:
            return {"status": "skip", "reason": "no eligible urls after policy checks"}

        ingest_summary = indexto_chroma_safe(question or "(no question)", "", items)
        return {
            "status": "ok",
            "urls_considered": len(urls),
            "urls_indexed": len(items),
            "ingest_summary": ingest_summary,
        }
    except Exception as e:
        log.debug("safe_auto_ingest_answer_links error: %s", e)
        return {"status": "error", "error": str(e)}
