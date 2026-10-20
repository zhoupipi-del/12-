import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="prodsync_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")  # 外部指定（如 MySQL）则不覆盖
os.environ["UPLOAD_DIR"] = f"{_tmp}/uploads"
os.environ["AI_ENABLED"] = "0"
os.environ["APP_ENV"] = "test"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app import seed
    from app.main import app
    seed.run()
    with TestClient(app) as c:
        yield c


def _login(client, username, password="123456"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="session")
def H(client):
    """各角色请求头。"""
    return {u: _login(client, u, "admin123" if u == "admin" else "123456")
            for u in ["admin", "zhuguan", "jihua", "caigou", "cangku", "zhijian", "chejian", "jishu", "shouhou",
                      "xiaoshou"]}
