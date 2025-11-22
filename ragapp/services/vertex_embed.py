# ragapp/services/vertex_embed.py
from __future__ import annotations
import os
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# =========================================================
# Vertex AI SDK 로드 (임베딩 + LLM)
# =========================================================
try:
    import vertexai
    from vertexai.vision_models import MultiModalEmbeddingModel, Image
    from vertexai.language_models import TextEmbeddingModel
except Exception:
    vertexai = None  # type: ignore
    MultiModalEmbeddingModel = None  # type: ignore
    Image = None  # type: ignore
    TextEmbeddingModel = None  # type: ignore

# LLM 전용 (버전이 낮아서 없어도 임베딩은 계속 동작하도록 분리)
try:
    from vertexai.generative_models import GenerativeModel  # type: ignore
except Exception:  # pragma: no cover
    GenerativeModel = None  # type: ignore

# =========================================================
# 환경 변수
# =========================================================
# 프로젝트/리전
PROJECT = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

# 임베딩 모델 이름 (.env 만 보고 결정)
MM_MODEL = os.getenv("VERTEX_MM_EMBED_MODEL", "multimodalembedding@001")
TXT_MODEL = os.getenv("VERTEX_TXT_EMBED_MODEL", "text-embedding-004")

# 기대 차원
MM_DIM = int(os.getenv("VERTEX_MM_EMBED_DIM", "1408"))
TXT_DIM_ENV = os.getenv("VERTEX_TXT_EMBED_DIM")
TXT_DIM = int(TXT_DIM_ENV) if (TXT_DIM_ENV or "").isdigit() else None

# 임베딩 L2 정규화 on/off (기본: 켜짐)
EMBED_L2_NORMALIZE = (
    os.getenv("EMBED_L2_NORMALIZE", "1").lower() not in ("0", "false", "no")
)

# RAG / 표 질의용 LLM 모델 이름 (Vertex GenerativeModel 에서 사용)
# 우선순위:
#   GEMINI_MODEL_TABLE > GEMINI_MODEL_RAG > GEMINI_MODEL_DIRECT > GEMINI_MODEL > GEMINI_TEXT_MODEL > VERTEX_TEXT_MODEL > 기본값
GEMINI_RAG_MODEL_ENV = (
    os.getenv("GEMINI_MODEL_RAG")
    or os.getenv("GEMINI_MODEL_DIRECT")
    or None
)

TABLE_LLM_MODEL = (
    os.getenv("GEMINI_MODEL_TABLE")
    or GEMINI_RAG_MODEL_ENV
    or os.getenv("GEMINI_MODEL")
    or os.getenv("GEMINI_TEXT_MODEL")
    or os.getenv("VERTEX_TEXT_MODEL")
    or "gemini-2.5-flash"
)

# =========================================================
# Vertex 공통 초기화 (임베딩/LLM 공통)
# =========================================================
def _init_once() -> None:
    if vertexai is None:
        raise RuntimeError(
            "google-cloud-aiplatform (vertexai) 패키지가 필요합니다. "
            "터미널에서 'pip install google-cloud-aiplatform' 후 다시 실행해 주세요."
        )
    if not PROJECT:
        raise RuntimeError(
            "VERTEX_PROJECT 또는 GOOGLE_CLOUD_PROJECT 환경변수가 필요합니다."
        )
    if not getattr(_init_once, "_done", False):
        vertexai.init(project=PROJECT, location=LOCATION)
        _init_once._done = True


_mm_model: Optional[Any] = None
_txt_model: Optional[Any] = None
_llm_model: Optional[Any] = None


def _mm() -> Any:
    """멀티모달 임베딩 모델 핸들 (이미지/텍스트 임베딩)."""
    global _mm_model
    _init_once()
    if MultiModalEmbeddingModel is None:
        raise RuntimeError(
            "MultiModalEmbeddingModel 클래스를 찾을 수 없습니다. "
            "google-cloud-aiplatform 버전을 확인해 주세요."
        )
    if _mm_model is None:
        _mm_model = MultiModalEmbeddingModel.from_pretrained(MM_MODEL)
    return _mm_model


def _txt() -> Any:
    """텍스트 임베딩 모델 핸들 (text-embedding-004)."""
    global _txt_model
    _init_once()
    if TextEmbeddingModel is None:
        raise RuntimeError(
            "TextEmbeddingModel 클래스를 찾을 수 없습니다. "
            "google-cloud-aiplatform 버전을 확인해 주세요."
        )
    if _txt_model is None:
        _txt_model = TextEmbeddingModel.from_pretrained(TXT_MODEL)
    return _txt_model


def _llm() -> Any:
    """
    표 질의 해석용 Vertex Gemini LLM 핸들.
    - 서비스 계정 JSON / ADC 로만 동작 (GOOGLE_API_KEY 필요 없음)
    """
    global _llm_model
    _init_once()
    if GenerativeModel is None:
        raise RuntimeError(
            "vertexai.generative_models.GenerativeModel 을 사용할 수 없습니다. "
            "google-cloud-aiplatform 버전 또는 환경을 확인해 주세요."
        )
    if _llm_model is None:
        _llm_model = GenerativeModel(TABLE_LLM_MODEL)
    return _llm_model


# =========================================================
# 유틸 (정규화)
# =========================================================
def _l2_norm(v: List[float]) -> List[float]:
    if not EMBED_L2_NORMALIZE:
        return v
    s = sum(x * x for x in v) ** 0.5
    if s == 0.0:
        return v
    inv = 1.0 / s
    return [x * inv for x in v]


def _l2_norm_many(vs: List[List[float]]) -> List[List[float]]:
    if not EMBED_L2_NORMALIZE:
        return vs
    return [_l2_norm(v) for v in vs]


# =========================================================
# 공개 API (임베딩)
# =========================================================
def embed_text_mm(text: str, dim: Optional[int] = None) -> List[float]:
    """
    멀티모달 '텍스트' 임베딩 (이미지 검색용 쿼리 벡터).
    - Vertex MultiModalEmbeddingModel.get_embeddings(text=..., dimension=...) 사용
    """
    if not text:
        raise ValueError("text is empty")

    d = int(dim or MM_DIM)
    try:
        mm = _mm()
        out = mm.get_embeddings(text=text, dimension=d)
        vec = list(out.text_embedding)
        if not vec:
            raise RuntimeError("빈 벡터가 반환되었습니다.")
        return _l2_norm(vec)
    except TypeError as e:
        # get_embeddings(text=...) 자체를 지원하지 않는 구버전 SDK
        raise RuntimeError(
            "현재 설치된 google-cloud-aiplatform 버전에서 "
            "MultiModalEmbeddingModel.get_embeddings(text=..., dimension=...) "
            "형식을 지원하지 않습니다.\n"
            "터미널에서 'pip install --upgrade google-cloud-aiplatform' 로 "
            "업그레이드한 뒤 다시 시도해 주세요."
        ) from e
    except Exception as e:
        raise RuntimeError(f"멀티모달 텍스트 임베딩 실패: {e}") from e


def embed_image_file(
    path: str,
    mime: Optional[str] = None,  # mime 는 현재는 로깅/확장용 이지만 시그니처 유지
    dim: Optional[int] = None,
) -> List[float]:
    """
    멀티모달 '이미지' 임베딩 (이미지 자체 벡터).
    """
    d = int(dim or MM_DIM)
    try:
        mm = _mm()
        if Image is None:
            raise RuntimeError(
                "vertexai.vision_models.Image 클래스를 찾을 수 없습니다."
            )
        img = Image.load_from_file(path)
        out = mm.get_embeddings(image=img, dimension=d)
        vec = list(out.image_embedding)
        if not vec:
            raise RuntimeError("빈 벡터가 반환되었습니다.")
        return _l2_norm(vec)
    except TypeError as e:
        raise RuntimeError(
            "현재 설치된 google-cloud-aiplatform 버전에서 "
            "MultiModalEmbeddingModel.get_embeddings(image=..., dimension=...) "
            "형식을 지원하지 않습니다.\n"
            "터미널에서 'pip install --upgrade google-cloud-aiplatform' 로 "
            "업그레이드한 뒤 다시 시도해 주세요."
        ) from e
    except Exception as e:
        raise RuntimeError(f"이미지 임베딩 실패: {e}") from e


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    텍스트 전용 임베딩(text-embedding-004).
    - 리스트 입력 → 리스트 출력 (List[List[float]])
    - TXT_DIM 이 설정되어 있으면 output_dimensionality 사용
    """
    if not texts:
        return []
    try:
        model = _txt()
        if TXT_DIM:
            embs = model.get_embeddings(texts, output_dimensionality=TXT_DIM)
        else:
            embs = model.get_embeddings(texts)
        vecs = [list(e.values) for e in embs]
        return _l2_norm_many(vecs)
    except Exception as e:
        raise RuntimeError(f"텍스트 임베딩 실패(text-embedding-004): {e}") from e


def embed_texts_vertex(texts: List[str]) -> List[List[float]]:
    """
    표 / CSV 전용 임베딩 헬퍼.
    내부적으로 embed_texts(...) 와 동일한 Vertex Text Embedding 설정을 사용합니다.
    """
    return embed_texts(texts)


def current_embed_dim(space: str = "mm") -> int:
    """
    space == 'mm'  -> 멀티모달 차원
    space == 'txt' -> 텍스트 임베딩 차원(환경에 지정 없으면 0)
    """
    if space.lower().startswith("txt"):
        return TXT_DIM or 0
    return MM_DIM


# =========================================================
# 표 질의용 LLM 헬퍼 (Vertex Gemini, 서비스 계정 JSON 사용)
# =========================================================
def infer_table_query_with_vertex(
    question: str,
    tables: Dict[str, Dict[str, Any]],
    default_table: Optional[str] = None,
) -> Dict[str, Any]:
    """
    자연어 질문 + 여러 개의 표 스키마를 Vertex Gemini 에 넘겨
    {table, filters, group_by, agg, agg_field} JSON 을 돌려주는 함수.

    feature_views.table_search_view 에서 기대하는 형식:
      {
        "table": "선택된 테이블 이름",
        "filters": [
          {"column": "region", "op": "=", "value": "서울"},
          {"column": "product", "op": "in", "value": ["아메리카노", "라떼"]}
        ],
        "group_by": "region",
        "agg": "sum",
        "agg_field": "sales"
      }

    - question : 사용자의 자연어 질문
    - tables   : {
        "매출표": {
          "columns": [...],
          "column_types": {...},
          "sample_rows": [{...}, ...]
        },
        ...
      }
    - default_table : 폼에서 사용자가 선택한 테이블(없을 수 있음)

    ⚠️ LLM 을 전혀 쓰고 싶지 않으면, 이 함수를 호출하는 쪽(feature_views)에서
       infer_table_query_with_vertex 가 None 이거나 {} 를 리턴하면 그냥 무시된다.
    """
    import json as _json
    import re as _re

    q = (question or "").strip()
    if not q:
        return {}
    if not tables:
        return {}

    # LLM 기능이 아예 없는 환경이면 바로 포기 (임베딩 + JSON fallback만 사용)
    if GenerativeModel is None or vertexai is None:
        log.warning(
            "Vertex LLM(GenerativeModel)이 없어서 infer_table_query_with_vertex 를 건너뜁니다."
        )
        return {}

    payload = {
        "tables": tables,
        "default_table": default_table,
    }

    # 프롬프트: 무조건 JSON 하나만, 조건/집계까지 정리해 달라고 요청
    prompt = (
        "너는 한국어로 된 질문을 표 분석용 구조화 쿼리로 바꿔주는 도우미야.\n"
        "아래 여러 개의 표 스키마와 예시 행들을 보고, 사용자의 질문을 만족하는 조건을 만들어라.\n"
        "반드시 아래 JSON 형식으로만, 다른 설명 없이 한 번만 출력해.\n\n"
        "형식:\n"
        "{\n"
        '  \"table\": \"사용할 테이블 이름(아래 tables 중 하나)\",\n'
        '  \"filters\": [\n'
        '    {\"column\": \"컬럼명\", \"op\": \"=|contains|in\", \"value\": \"값 또는 값 목록\"}\n'
        "  ],\n"
        '  \"group_by\": \"그룹으로 묶을 컬럼명 또는 빈 문자열\",\n'
        '  \"agg\": \"count|sum|avg|min|max 또는 빈 문자열\",\n'
        '  \"agg_field\": \"집계에 사용할 숫자 컬럼명 또는 빈 문자열\"\n'
        "}\n\n"
        "규칙:\n"
        "- filters 에서 op 는 '=', 'contains', 'in' 중 하나만 사용한다.\n"
        "- 예) \"서울 지역 매출만\" → column:\"region\", op:\"=\", value:\"Seoul\".\n"
        "- 예) \"서울이랑 부산 합쳐서\" → column:\"region\", op:\"in\", value:[\"Seoul\",\"Busan\"].\n"
        "- 어떤 값이 확실하지 않으면 filters 를 비우고, agg / group_by / agg_field 는 빈 문자열로 둔다.\n"
        "- 여러 표 중 어디를 써야 할지 애매하면 default_table 이 있으면 우선 사용하고, 없으면 table 을 빈 문자열로 둔다.\n"
        "- JSON 말고 다른 텍스트는 절대 쓰지 마라.\n\n"
        f"[tables 및 기본 정보]\n{_json.dumps(payload, ensure_ascii=False)}\n\n"
        f"[사용자 질문]\n{q}\n"
    )

    try:
        model = _llm()
        resp = model.generate_content(prompt)

        # SDK 버전에 따라 resp.text 또는 candidates[0].content.parts 로 나올 수 있음
        text = getattr(resp, "text", None)
        if not text and getattr(resp, "candidates", None):
            try:
                parts = resp.candidates[0].content.parts
                text = "".join(getattr(p, "text", "") for p in parts)
            except Exception:
                text = None
        if not text:
            return {}

        # 응답에서 JSON 부분만 추출
        m = _re.search(r"\{.*\}", text, _re.S)
        if m:
            text = m.group(0)

        data = _json.loads(text)
        if not isinstance(data, dict):
            return {}

        # 기본 필드 채우기
        data.setdefault("table", default_table or "")
        data.setdefault("filters", [])
        data.setdefault("group_by", "")
        data.setdefault("agg", "")
        data.setdefault("agg_field", "")

        # table 정합성 체크
        table_name = str(data.get("table") or "").strip()
        if table_name and table_name not in tables:
            # LLM 이 이상한 이름을 준 경우 → default_table 이 유효하면 그걸로, 아니면 공백
            if default_table and default_table in tables:
                data["table"] = default_table
            else:
                data["table"] = ""
        elif (not table_name) and default_table and default_table in tables:
            data["table"] = default_table

        # agg 정리
        agg = str(data.get("agg") or "").lower()
        allowed_agg = {"", "count", "sum", "avg", "min", "max"}
        if agg not in allowed_agg:
            data["agg"] = ""

        # filters 는 리스트만 허용
        filters = data.get("filters")
        if not isinstance(filters, list):
            data["filters"] = []
        else:
            # dict 아닌 항목 제거
            data["filters"] = [f for f in filters if isinstance(f, dict)]

        # 문자열 필드 보정
        data["group_by"] = str(data.get("group_by") or "")
        data["agg_field"] = str(data.get("agg_field") or "")

        return data
    except Exception as e:
        # 여기서 예외가 나도, 표 검색 전체가 죽지 않고 그냥 LLM 보조만 끄는 쪽으로
        log.exception("infer_table_query_with_vertex 실패: %s", e)
        return {}
