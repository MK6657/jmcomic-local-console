from . import database as db
from .background_calls import BackgroundCallsStopped
from .jm_service import get_album_detail_cached
from .logger import log
from .tag_jobs import tag_jobs
from .upstream import UpstreamFailure


def save_tags(album_id, detail):
    raw = detail.get("tags")
    if not isinstance(raw, list):
        raise UpstreamFailure("invalid_response", "上游标签列表无效")
    names = [tag.get("name") if isinstance(tag, dict) else tag for tag in raw]
    if any(not isinstance(name, str) for name in names):
        raise UpstreamFailure("invalid_response", "上游标签名称无效")
    tag_jobs.require_accepting()
    return db.batch_sync_auto_tags(album_id, names, detail=detail)


def sync_one(album_id, detail=None):
    tag_jobs.require_accepting()
    try:
        save_tags(album_id, detail if detail is not None else get_album_detail_cached(album_id))
    except BackgroundCallsStopped:
        raise
    except Exception as exc:
        log.warning(f"同步标签失败 album_id={album_id} error={exc}")
        return False
    return True


def sync_all():
    cursor = ""
    processed = errors = 0
    try:
        while True:
            tag_jobs.require_accepting()
            ids = db.library_ids_after(cursor)
            if not ids:
                break
            for album_id in ids:
                if sync_one(album_id):
                    processed += 1
                else:
                    errors += 1
            cursor = ids[-1]
    except BackgroundCallsStopped:
        log.info(f"批量同步标签已停止: processed={processed}, errors={errors}")
        return
    except Exception as exc:
        log.error(f"后台批量同步标签失败 error={exc}")
        return
    log.info(f"批量同步标签完成: processed={processed}, errors={errors}")
