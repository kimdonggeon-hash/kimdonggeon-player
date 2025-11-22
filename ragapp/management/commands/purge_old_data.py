from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Model, QuerySet
from django.utils import timezone


def _get_cutoff(days: int) -> timezone.datetime:
    now = timezone.now()
    return now - timedelta(days=int(days))


def _find_datetime_field(model: type[Model]) -> Optional[str]:
    """
    created_at / updated_at / timestamp / created 등의 흔한 필드명 중
    존재하는 것을 우선순위대로 반환. 없으면 None.
    """
    candidates = ["created_at", "updated_at", "timestamp", "created", "time", "date"]
    fields = {f.name for f in model._meta.get_fields()}  # type: ignore[attr-defined]
    for name in candidates:
        if name in fields:
            return name
    return None


def _purge_model(
    model: type[Model],
    cutoff,
    *,
    field_name: Optional[str] = None,
    dry_run: bool = False,
) -> int:
    """
    주어진 모델에서 cutoff 이전 레코드를 삭제. 삭제 개수 반환.
    - field_name 미지정 시 자동 추정( created_at → updated_at → timestamp ... )
    """
    fn = field_name or _find_datetime_field(model)
    if not fn:
        return 0  # 기준 필드 없으면 건너뜀

    qs: QuerySet = model.objects.filter(**{f"{fn}__lt": cutoff})
    count = qs.count()
    if not dry_run and count:
        qs.delete()
    return count


class Command(BaseCommand):
    help = "RETENTION_DAYS 기준으로 오래된 로그/기록과 (옵션) Chroma 벡터를 정리합니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            help="보관일 덮어쓰기(기본: settings.RETENTION_DAYS). 0 이하면 동작 안함.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="실제 삭제하지 않고 삭제 예정 건수만 출력",
        )
        parser.add_argument(
            "--no-chroma",
            action="store_true",
            help="Chroma(벡터DB) 정리는 건너뜀",
        )
        parser.add_argument(
            "--models",
            nargs="*",
            choices=["chat", "mylog", "feedback", "ingest"],
            help="정리할 모델 서브셋만 지정 (미지정 시 전부 실행)",
        )

    def handle(self, *args, **opts):
        days = opts.get("days")
        if days is None:
            days = int(getattr(settings, "RETENTION_DAYS", 0) or 0)

        dry_run: bool = bool(opts.get("dry_run"))
        no_chroma: bool = bool(opts.get("no_chroma"))
        only_models = set(opts.get("models") or [])

        if days <= 0:
            self.stdout.write(self.style.WARNING("RETENTION_DAYS<=0: 아무 작업도 수행하지 않습니다."))
            return

        cutoff = _get_cutoff(days)
        self.stdout.write(self.style.NOTICE(f"보관기간: {days}일, 기준시각(cutoff): {cutoff.isoformat()}"))
        if dry_run:
            self.stdout.write(self.style.WARNING("드라이런 모드: 실제 삭제하지 않습니다."))

        # ── Django 모델 정리 ─────────────────────────────────────
        deleted_total = 0

        # Import를 try로 감싸 모델 부재 시 무시
        # 1) ChatQueryLog
        if not only_models or "chat" in only_models:
            try:
                from ragapp.models import ChatQueryLog  # type: ignore
                n = _purge_model(ChatQueryLog, cutoff, field_name="created_at", dry_run=dry_run)
                deleted_total += n
                self.stdout.write(f"- ChatQueryLog: {n}건 {'삭제 예정' if dry_run else '삭제 완료'}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"- ChatQueryLog 건너뜀: {e}"))

        # 2) MyLog
        if not only_models or "mylog" in only_models:
            try:
                from ragapp.models import MyLog  # type: ignore
                n = _purge_model(MyLog, cutoff, field_name="created_at", dry_run=dry_run)
                deleted_total += n
                self.stdout.write(f"- MyLog: {n}건 {'삭제 예정' if dry_run else '삭제 완료'}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"- MyLog 건너뜀: {e}"))

        # 3) Feedback
        if not only_models or "feedback" in only_models:
            try:
                from ragapp.models import Feedback  # type: ignore
                n = _purge_model(Feedback, cutoff, field_name="created_at", dry_run=dry_run)
                deleted_total += n
                self.stdout.write(f"- Feedback: {n}건 {'삭제 예정' if dry_run else '삭제 완료'}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"- Feedback 건너뜀: {e}"))

        # 4) IngestHistory
        if not only_models or "ingest" in only_models:
            try:
                from ragapp.models import IngestHistory  # type: ignore
                # created_at이 없다면 updated_at/created 등 자동탐색
                n = _purge_model(IngestHistory, cutoff, field_name="created_at", dry_run=dry_run)
                deleted_total += n
                self.stdout.write(f"- IngestHistory: {n}건 {'삭제 예정' if dry_run else '삭제 완료'}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"- IngestHistory 건너뜀: {e}"))

        # ── Chroma(벡터DB) 정리 ─────────────────────────────────
        # 메타데이터의 "ingested_at" < cutoff 인 벡터 삭제
        chroma_deleted = None
        if not no_chroma:
            try:
                from ragapp.services.chroma_store import chroma_collection as _chroma_collection  # type: ignore
                col = _chroma_collection()

                # 일부 Chroma 버전은 숫자/문자열 비교를 지원.
                # ISO 8601 문자열은 사전순 정렬이 시간순과 일치하므로 $lt 비교 가능.
                where = {"ingested_at": {"$lt": cutoff.isoformat()}}
                before = None
                after = None
                try:
                    before = col.count()
                except Exception:
                    pass

                if dry_run:
                    # 드라이런: delete 실행 대신 get(where=...)로 매칭 개수 추정 시도
                    to_delete = 0
                    try:
                        res = col.get(where=where, include=[])
                        ids = (res or {}).get("ids") or []
                        to_delete = len(ids)
                    except Exception:
                        # get(where=...) 미지원 버전이면 개수 추정 불가
                        to_delete = -1
                    chroma_deleted = to_delete
                    if to_delete >= 0:
                        self.stdout.write(f"- Chroma: {to_delete}개 벡터 삭제 예정")
                    else:
                        self.stdout.write("- Chroma: 드라이런 추정 불가(버전 미지원). 실제 실행 시 삭제됩니다.")
                else:
                    col.delete(where=where)  # 지원 안하면 예외 발생 → except에서 로그 후 무시
                    chroma_deleted = -1  # 정확 수량 모를 수 있음

                try:
                    after = col.count()
                except Exception:
                    pass

                if not dry_run:
                    # 정확한 삭제 수를 모를 수 있어 before/after로 추정
                    if before is not None and after is not None:
                        chroma_deleted = max(0, before - after)
                    self.stdout.write(f"- Chroma: {chroma_deleted if chroma_deleted is not None else '?'}개 삭제 완료")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"- Chroma 정리 건너뜀: {e}"))

        # ── 요약 ────────────────────────────────────────────────
        if dry_run:
            self.stdout.write(self.style.WARNING(f"[DRY-RUN] Django 모델 삭제 예정 합계: {deleted_total}건"))
            if chroma_deleted is not None:
                self.stdout.write(self.style.WARNING(f"[DRY-RUN] Chroma 삭제 예정: {chroma_deleted if chroma_deleted >= 0 else '추정 불가'}개"))
        else:
            self.stdout.write(self.style.SUCCESS(f"삭제 완료. Django 모델 합계: {deleted_total}건"))
            if chroma_deleted is not None:
                self.stdout.write(self.style.SUCCESS(f"Chroma 삭제: {chroma_deleted if chroma_deleted >= 0 else '완료(개수 추정 불가)'}"))
