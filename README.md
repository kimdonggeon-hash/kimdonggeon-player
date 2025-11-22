# ragsite

# 이번 프로젝트는 100% chat gpt를 활용하여서 공부 하면서 내 능력을 최대한 올리는게 목적이였습니다

# RAG 통합 검색 콘솔 (kimdonggeon-player)

Django 기반으로 만든 **뉴스/문서 RAG 검색 + 실시간 상담 콘솔 + 로그/동의 관리** 프로젝트입니다.  
테스터가 웹에서 질문을 넣으면, 수집한 뉴스/문서/멀티모달 임베딩을 이용해 답변을 생성하고,  
필요하면 상담사와 실시간 채팅(라이브챗)으로 이어주는 구조입니다.

---

## 주요 기능

- **RAG 웹 검색**
  - 뉴스/문서 인덱싱 후, 사용자가 입력한 질문에 관련 문서 기반 답변 생성
  - 검색 기록/피드백 버튼(유용/별로)을 통한 품질 로그 저장

- **멀티모달 / 벡터 스토어**
  - 텍스트/표/이미지 등 임베딩 후 벡터 스토어(Chroma / SQLite / 등) 저장
  - 뉴스/문서 검색, 미디어 검색 등의 백엔드로 사용

- **실시간 상담 (Live Chat)**
  - 사용자 페이지(QARAG 질문 챗봇)와 어드민 상담사 콘솔 간 실시간 채팅
  - 상담 세션 시작/종료, 상담 이력 관리

- **법무/동의 관리**
  - 개인정보처리방침, 이용약관, 국외이전, 테스터 동의서 등 렌더링
  - 동의 로그(ConsentLog), 피드백 로그(FeedbackLog) 별도 저장 디렉토리 관리

- **관리자(Admin) 화면**
  - 뉴스 크롤링 & 인덱싱 콘솔
  - 업로드 문서 인덱싱 뷰
  - RAG 설정, 로그 조회 등

---

## 기술 스택

- **Backend**
  - Python 3.10+
  - Django
  - Django Admin
- **AI / RAG**
  - Vertex AI Embeddings / Gemini (텍스트/멀티모달)
  - Chroma / SQLite / 기타 벡터 스토어
- **Frontend**
  - 기본 HTML/CSS/JS (뉴스 검색 화면, QARAG 챗 UI, 라이브챗 콘솔)
- **기타**
  - Git / GitHub
  - 가상환경: `venv` (`.venv` 폴더)

---

## 폴더 구조 (요약)

> 실제 폴더는 일부 다를 수 있으니 참고용입니다.

```text
project_root/
├─ manage.py
├─ ragsite/                # Django 프로젝트 설정 (settings, urls, asgi 등)
├─ ragapp/                 # 핵심 앱: RAG, 뉴스, 라이브챗, 로그 모델 등
│  ├─ templates/
│  │  ├─ ragapp/news.html          # 메인 검색/챗 화면
│  │  └─ ragadmin/...              # 관리자용 템플릿
│  ├─ static/ragapp/               # CSS, JS, 이미지 등
│  ├─ services/                    # RAG, 임베딩, 크롤러 유틸
│  └─ livechat/                    # 실시간 상담 관련 코드
├─ scripts/                # 크롤링, 인덱싱, 마이그레이션 스크립트
├─ requirements.txt        # Python 패키지 목록
├─ .env.example            # 환경 변수 예시 (실제 키는 여기에 넣지 않음)
└─ README.md
