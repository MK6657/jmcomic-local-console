"""Page confirmations for a download that hands its slot back to the queue."""
import hashlib
import json
from pathlib import Path
from contextlib import closing

from . import database as db
from .file_tree import is_link
from .logger import log
from .storage import is_temporary_path, staged_file


def _checkpoint_path(job_id):
    key = hashlib.sha256(job_id.encode()).hexdigest()
    return db.DB_DIR / f"resume-{key}.json"


def discard_checkpoint(job_id):
    try:
        _checkpoint_path(job_id).unlink(missing_ok=True)
    except OSError as error:
        log.warning(f"清理续传记录失败 job_id={job_id} error={error}")


class DownloadCheckpoint:
    def __init__(self, job_id, row):
        self.job_id = job_id
        self.previous_total = row.get('total_pages') or 0
        self.previous_done = row.get('done_pages') or 0
        self.directory = Path(row['output_path']) if self.previous_total and row.get('output_path') else None
        self.pages = {}
        self.confirmed = {}
        self.dirty = False
        try:
            pages = json.loads(_checkpoint_path(job_id).read_bytes())
            if isinstance(pages, dict):
                self.pages = pages
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            log.warning(f"读取续传记录失败，将重新核对页面 job_id={job_id} error={error}")
        if not self.pages:
            self.previous_done = 0

    def prepare_directory(self, root):
        """Recreate the recorded path under this root, without following links or searching for a moved book."""
        directory = self.directory
        try:
            parts = directory.relative_to(root).parts
        except ValueError:
            parts = ()
        if not parts or '..' in parts or is_temporary_path(directory):
            raise ValueError("无法续传：原输出目录必须是当前下载根目录内的普通子目录")
        current = root
        try:
            for part in ('', *parts):
                current = current / part
                try:
                    linked = is_link(current)
                except FileNotFoundError:
                    current.mkdir(exist_ok=True)
                    linked = is_link(current)
                if linked:
                    raise ValueError("无法续传：原输出路径包含链接或目录联接")
                if not current.is_dir():
                    raise ValueError("无法续传：原输出路径已被文件占用")
        except OSError as error:
            raise ValueError(f"无法续传：无法检查或重建原输出目录：{error}") from error
        return directory

    def _key(self, path):
        return path.relative_to(self.directory).as_posix()

    @staticmethod
    def _signature(path):
        info = path.stat()
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    def contains(self, path):
        if getattr(self, 'isolated', False):
            if not path.is_file() or is_link(path):
                return False
            expected = self.receipts.get(self._key(path))
            return expected is not None and expected == self.digest(path)
        expected = self.pages.get(self._key(path))
        if expected is None:
            return False
        try:
            return expected == self._signature(path)
        except OSError:
            return False

    def confirm(self, path, verified_digest=None):
        """Called under the page counter lock, after image verification or publication."""
        key = self._key(path)
        signature = self._signature(path)
        if getattr(self, 'isolated', False):
            digest = verified_digest if verified_digest is not None else self.digest(path)
            if verified_digest is None:
                self.remember_digest(path, digest)
            if self.receipts.get(key) != digest:
                raise OSError('页面与下载确认摘要不一致')
            self.confirmed[(path.parent, path.stem)] = (path, digest)
        if self.pages.get(key) != signature:
            self.pages[key] = signature
            self.dirty = True

    def receipt_digest(self, path):
        return self.receipts[self._key(path)]

    @staticmethod
    def digest(path):
        with path.open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest()

    def own(self, directory):
        self.directory, self.isolated = directory, True
        with closing(db.get_db()) as conn:
            self.receipts = dict(conn.execute('SELECT page,digest FROM task_page_receipts WHERE job_id=?', (self.job_id,)))

    def remember_digest(self, path, digest):
        key = self._key(path)
        if self.receipts.get(key) != digest:
            with db.transaction() as conn:
                conn.execute('INSERT OR REPLACE INTO task_page_receipts VALUES(?,?,?)', (self.job_id, key, digest))
            self.receipts[key] = digest

    def retire(self, status):
        """Called after all writers drain, under the manager's per-job command lock."""
        if status not in ('paused', 'queued', 'running'):
            discard_checkpoint(self.job_id)
        elif self.dirty:
            payload = json.dumps(self.pages, ensure_ascii=False, separators=(',', ':')).encode()
            with staged_file(_checkpoint_path(self.job_id)) as staged:
                staged.path.write_bytes(payload)
                staged.publish(lambda path: path.stat().st_size == len(payload))
