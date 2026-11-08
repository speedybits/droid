import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DROID_DATA", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # so create_app() doesn't pick up the real .env


def four_rooms_world(size=8.0, res=0.1) -> dict:
    """Same layout as nimbus's four_rooms (outer walls, cross wall, four 0.8 m doors), no random obstacles."""
    n = int(size / res)
    mid = n // 2
    g = [[False] * n for _ in range(n)]  # g[gy][gx], True = wall
    for i in range(n):
        g[0][i] = g[n - 1][i] = g[i][0] = g[i][n - 1] = g[i][mid] = g[mid][i] = True
    for d in range(8):
        g[mid + mid // 2 + d][mid] = False
        g[mid // 4 + d][mid] = False
        g[mid][mid // 4 + d] = False
        g[mid][mid + mid // 2 + d] = False
    return {"width": n, "height": n, "resolution": res, "origin_x": size / 2, "origin_y": size / 2,
            "rows": ["".join("#" if c else "." for c in row) for row in g]}


@pytest.fixture
def world():
    return four_rooms_world()
