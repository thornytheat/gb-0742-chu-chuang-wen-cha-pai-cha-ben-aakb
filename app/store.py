import json
import sqlite3
from contextlib import closing, contextmanager

from app.errors import FieldError
from app.manifest import canonical


class ArchiveStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            yield connection

    def setup(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS shift_archives (
                    id TEXT PRIMARY KEY,
                    manifest TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    input TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS retention_batches (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created TEXT NOT NULL
                )
            """)

    def insert(self, manifest, evidence, source):
        with self.connect() as connection:
            cursor = connection.execute("INSERT INTO shift_archives VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                                        (manifest["id"], canonical(manifest), canonical(evidence), canonical(source)))
            record = connection.execute("SELECT manifest FROM shift_archives WHERE id = ?", (manifest["id"],)).fetchone()
            return json.loads(record[0]), cursor.rowcount == 1

    def fetch(self, run_id):
        with self.connect() as connection:
            record = connection.execute("SELECT manifest, evidence, input FROM shift_archives WHERE id = ?", (run_id,)).fetchone()
        if record is None:
            raise FieldError("UNKNOWN_RUN", "该归卷未登记", 404)
        return tuple(json.loads(value) for value in record)

    def history(self):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT manifest FROM shift_archives ORDER BY rowid DESC")]

    def all_archives(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT manifest, evidence, input FROM shift_archives ORDER BY rowid").fetchall()
        return [tuple(json.loads(value) for value in row) for row in rows]

    def fetch_batch(self, batch_id):
        with self.connect() as connection:
            record = connection.execute("SELECT payload FROM retention_batches WHERE id = ?", (batch_id,)).fetchone()
        if record is None:
            raise FieldError("UNKNOWN_BATCH", "该结转批次未登记", 404)
        return json.loads(record[0])

    def list_batches(self):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in
                    connection.execute("SELECT payload FROM retention_batches ORDER BY rowid DESC")]

    def commit_retention(self, batch, downgrades):
        """同一事务提交批次结论与全部降级写入；任一步失败整体回滚。"""
        with self.connect() as connection:
            existing = connection.execute("SELECT 1 FROM retention_batches WHERE id = ?", (batch["id"],)).fetchone()
            if existing:
                return False
            connection.execute("INSERT INTO retention_batches VALUES (?, ?, ?)",
                               (batch["id"], canonical(batch), batch["created"]))
            for run_id, evidence, source in downgrades:
                connection.execute("UPDATE shift_archives SET evidence = ?, input = ? WHERE id = ?",
                                   (canonical(evidence), canonical(source), run_id))
        return True
