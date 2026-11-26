"""The schema file and the schema doc are generated; these tests stop them drifting from the migrations."""
import os
import pathlib
import sys
import unittest
import uuid

import psycopg

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_schema  # noqa: E402
import schema_doc  # noqa: E402

from app.main import create_app  # noqa: E402
from app.migrate import migrate  # noqa: E402

DSN = os.environ.get("TEST_PG_DSN")


class TestSchemaFiles(unittest.TestCase):
    def test_schema_sql_is_up_to_date(self):  # no database needed
        self.assertEqual((ROOT / "sql" / "schema.sql").read_text(), build_schema.build(),
                         "stale: run  python tools/build_schema.py")


@unittest.skipUnless(DSN, "set TEST_PG_DSN to run the tests against a Postgres database")
class TestSchemaOnDatabase(unittest.TestCase):
    def setUp(self):
        self.schemas = []

    def tearDown(self):
        with psycopg.connect(DSN, autocommit=True) as c:
            for s in self.schemas:
                c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")

    def scratch(self):
        s = "t_" + uuid.uuid4().hex[:12]
        self.schemas.append(s)
        return s

    def test_schema_doc_is_up_to_date(self):
        self.assertEqual((ROOT / "docs" / "schema.md").read_text(), schema_doc.build(DSN),
                         "stale: run  PG_DSN=... python tools/schema_doc.py")

    def test_every_table_and_column_is_documented(self):
        s = self.scratch()
        migrate(DSN, s)
        with psycopg.connect(DSN) as c:
            d = schema_doc.describe(c, s)
        self.assertTrue(d["tabs"])
        self.assertEqual([t for t, com in d["tabs"] if not com], [], "tables without a COMMENT")
        self.assertEqual([f"{r[0]}.{r[2]}" for r in d["cols"] if not r[8]], [], "columns without a COMMENT")

    def test_schema_sql_by_hand_equals_the_migrations(self):
        a, b = self.scratch(), self.scratch()
        migrate(DSN, a)
        with psycopg.connect(DSN, autocommit=True) as c:  # what the header says to do with psql
            c.execute(f"CREATE SCHEMA {b}")
            c.execute(f"SET search_path TO {b}")
            c.execute((ROOT / "sql" / "schema.sql").read_text())
        with psycopg.connect(DSN) as c:
            self.assertEqual(schema_doc.render(schema_doc.describe(c, a), a), schema_doc.render(schema_doc.describe(c, b), b))
        self.assertEqual(migrate(DSN, b), [], "schema.sql must record the migrations as applied")
        app = create_app(DSN, b)  # and the API starts fine on a hand-built database
        app.state.db.close()


if __name__ == "__main__":
    unittest.main()
