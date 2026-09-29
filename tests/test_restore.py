"""误操作恢复（已分配地址恢复为历史快照地址集合）接口的端到端测试。

直接运行：``python3 -m tests.test_restore``（或 pytest 收集）。

造数：父网段 10.10.0.0/16，初始保留 10.10.0.0/20 与 10.10.255.0/24。

覆盖：
* 成功恢复：仅已分配地址回到历史快照的地址集合，保留区保持当前状态；
* 恢复后的已分配地址规范化为数量最少的规范 CIDR（相邻多条记录合并）；
* 恢复生成新版本与不可变快照，历史快照本身不改变；
* 恢复后 GET /api/pool、后续预演/提交立即看到恢复后的状态；
* 历史版本不存在（未来版本/0 之前）返回 404，绝不以当前状态代替；
* 历史已分配地址与当前保留区相交（完全重叠/部分相交）返回 409；
* 当前版本过期返回 409；404/409 时池与版本均不变；
* 恢复到当前快照（占用集不变）也生成新版本；
* 非法请求体（版本号非正整数/缺字段）返回 422；
* 同版本并发恢复：写锁互斥，恰有一个成功。
"""
import os
import tempfile
import threading

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_restore.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

INITIAL_RESERVED = ["10.10.0.0/20", "10.10.255.0/24"]


def _pool():
    return client.get("/api/pool").json()


def _version(version):
    return client.get(f"/api/pool/versions/{version}")


def _restore(pool_version, snapshot_version):
    return client.post(
        "/api/allocations/restore",
        json={
            "pool_version": pool_version,
            "snapshot_version": snapshot_version,
        },
    )


def _preview(prefixes):
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": prefixes}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True, body
    return body


def _commit(version, allocations):
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": version, "allocations": allocations},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _replace(version, desired):
    r = client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": version, "desired_cidrs": desired},
    )
    assert r.status_code == 200, r.text
    return r.json()


def main():
    pool = _pool()
    assert pool["version"] == 1
    assert pool["reserved_cidrs"] == INITIAL_RESERVED
    assert pool["allocated_cidrs"] == []

    # ---- 造历史：v2 分配 10.10.254.0/24 ----
    plan = _preview([24])
    assert plan["pool_version"] == 1
    a1 = plan["allocations"]
    assert a1 == ["10.10.254.0/24"], plan
    committed = _commit(1, a1)
    assert committed["pool_version"] == 2
    v2 = _version(2).json()
    assert v2["allocated_cidrs"] == a1

    # ---- v3 再分配 10.10.252.0/24（与 a1 不相邻）----
    plan = _preview([24])
    assert plan["pool_version"] == 2
    a2 = plan["allocations"]
    assert a2 == ["10.10.252.0/24"], plan
    committed = _commit(2, a2)
    assert committed["pool_version"] == 3
    v3 = _version(3).json()
    assert v3["allocated_cidrs"] == ["10.10.252.0/24", "10.10.254.0/24"]

    # ---- v4 再分配 10.10.253.0/24：与前两条相邻，三者可折叠为 ----
    # 10.10.252.0/23 + 10.10.254.0/24。
    plan = _preview([24])
    assert plan["pool_version"] == 3
    a3 = plan["allocations"]
    assert a3 == ["10.10.253.0/24"], plan
    committed = _commit(3, a3)
    assert committed["pool_version"] == 4
    v4 = _version(4).json()
    assert v4["allocated_cidrs"] == [
        "10.10.252.0/24",
        "10.10.253.0/24",
        "10.10.254.0/24",
    ]
    print("history v1..v4 prepared")

    # ---- 误操作：v5 再分配一条，随后恢复到 v2 ----
    plan = _preview([24])
    assert plan["pool_version"] == 4
    a_mistake = plan["allocations"]
    committed = _commit(4, a_mistake)
    assert committed["pool_version"] == 5
    before_restore = _pool()
    assert a_mistake[0] in before_restore["allocated_cidrs"]

    r = _restore(5, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "restored"
    assert body["pool_version"] == 6
    assert body["restored_from_version"] == 2
    # 已分配地址恢复为 v2 快照的地址集合。
    assert body["allocated_cidrs"] == v2["allocated_cidrs"] == [
        "10.10.254.0/24"
    ]
    # 当前保留区保持原样。
    pool = _pool()
    assert pool["version"] == 6
    assert pool["reserved_cidrs"] == INITIAL_RESERVED
    assert pool["allocated_cidrs"] == ["10.10.254.0/24"]
    assert pool["free_cidrs"] == body["free_cidrs"]
    assert "10.10.254.0/24" not in pool["free_cidrs"]
    assert "10.10.252.0/24" not in pool["allocated_cidrs"]
    print("restore to v2: allocated set reverted, reservations kept ok")

    # ---- 新版本快照：v6 内容等于 v2（同 parent/保留/已分配）----
    v6 = _version(6).json()
    assert v6["version"] == 6
    assert v6["parent_cidr"] == v2["parent_cidr"]
    assert v6["reserved_cidrs"] == v2["reserved_cidrs"]
    assert v6["allocated_cidrs"] == v2["allocated_cidrs"]
    assert v6["free_cidrs"] == v2["free_cidrs"]

    # ---- 历史快照本身不改变：v2/v4 逐字节保持生成时内容 ----
    assert _version(2).json() == v2
    assert _version(4).json() == v4
    print("restore creates v6 snapshot; historical snapshots unchanged ok")

    # ---- 恢复后预演/提交立即看到恢复后的状态：----
    # 10.10.252.0/24 重新空闲，单条 /24 最优落位该处（与 v3 前一致）。
    plan = _preview([24])
    assert plan["pool_version"] == 6
    assert plan["allocations"] == ["10.10.252.0/24"], plan
    committed = _commit(6, plan["allocations"])
    assert committed["pool_version"] == 7
    assert committed["allocations"] == ["10.10.252.0/24"]
    print("preview/commit see restored state immediately ok")

    # ---- 恢复时规范化为最少 CIDR：从 v4 恢复，三条相邻 /24 ----
    # 折叠为 10.10.252.0/23 + 10.10.254.0/24，且地址集合与 v4 相同。
    r = _restore(7, 4)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 8
    assert body["allocated_cidrs"] == [
        "10.10.252.0/23",
        "10.10.254.0/24",
    ], body
    pool = _pool()
    assert pool["allocated_cidrs"] == body["allocated_cidrs"]
    assert pool["free_cidrs"] == body["free_cidrs"]
    # v4 快照本身仍是生成时的三条记录（快照不被规范化重写）。
    assert _version(4).json() == v4
    # 折叠结果覆盖的地址集合与 v4 完全相同。
    import ipaddress

    def _address_set(cidrs):
        out = set()
        for raw in cidrs:
            out.update(ipaddress.ip_network(raw))
        return out

    assert _address_set(body["allocated_cidrs"]) == _address_set(
        v4["allocated_cidrs"]
    )
    print("restored allocated normalized to minimal CIDRs ok")

    # ---- 历史已分配地址与当前保留区相交 -> 409，池与版本不变 ----
    # 当前 v8 占用 10.10.252.0/23 + 10.10.254.0/24。把保留区替换为
    # 恰好压住 10.10.254.0/24 的 10.10.254.0/24（与当前占用相交，不能
    # 直接替换），因此先把已分配恢复到空占用快照 v1，再替换保留区。
    r = _restore(8, 1)
    assert r.status_code == 200, r.text
    assert r.json()["pool_version"] == 9
    assert r.json()["allocated_cidrs"] == []
    replaced = _replace(9, ["10.10.254.0/24"])
    assert replaced["pool_version"] == 10
    assert replaced["reserved_cidrs"] == ["10.10.254.0/24"]
    before = _pool()
    assert before["version"] == 10

    # v4 快照的已分配地址（252/253/254 三个 /24）与当前保留区
    # 10.10.254.0/24 相交（完全重叠其中一段）-> 409。
    r = _restore(10, 4)
    assert r.status_code == 409, r.text
    assert "overlap" in r.json()["detail"]
    assert _pool() == before
    # v2 快照的已分配地址恰好就是当前保留块本身 -> 同样 409。
    r = _restore(10, 2)
    assert r.status_code == 409, r.text
    assert "overlap" in r.json()["detail"]
    assert _pool() == before
    # 部分相交：直接提交一个落在空闲区的分配，再把保留区扩到压住它的
    # 一部分。10.10.16.0/24 在当前保留区之外且空闲。
    committed = _commit(10, ["10.10.16.0/24"])
    assert committed["pool_version"] == 11
    snapshot_partial = 11
    # 清空占用后替换保留区为压住该 /24 左半的 /25。恢复来源用 v10
    # 快照：它的保留区与当前相同（10.10.254.0/24）、占用为空，因此
    # 恢复后保留区不变、仅占用清空。
    assert _version(10).json()["reserved_cidrs"] == ["10.10.254.0/24"]
    r = _restore(11, 10)
    assert r.status_code == 200, r.text
    assert r.json()["allocated_cidrs"] == []
    assert r.json()["pool_version"] == 12
    assert _pool()["reserved_cidrs"] == ["10.10.254.0/24"]
    replaced = _replace(
        12, ["10.10.16.0/25", "10.10.254.0/24"]
    )
    assert replaced["pool_version"] == 13
    assert replaced["reserved_cidrs"] == [
        "10.10.16.0/25",
        "10.10.254.0/24",
    ]
    before = _pool()
    r = _restore(13, snapshot_partial)
    assert r.status_code == 409, r.text
    assert "overlap" in r.json()["detail"]
    assert _pool() == before
    print("historical allocated ∩ current reserved -> 409, untouched ok")

    # ---- 当前版本过期 -> 409，池与版本不变 ----
    r = _restore(10, 1)  # 当前是 13，10 已过期
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]
    assert _pool() == before
    print("stale current version -> 409, untouched ok")

    # ---- 历史版本不存在 -> 404（绝不以当前状态代替），池不变 ----
    # 正整数版本号但快照不存在（未来版本）-> 404；0/负数由模型约束 422。
    for missing in (14, 999):
        r = _restore(13, missing)
        assert r.status_code == 404, (missing, r.status_code, r.text)
        assert "unknown pool version" in r.json()["detail"]
    assert _pool() == before
    print("missing snapshot -> 404, never substituted by current state ok")

    # ---- 恢复到与当前占用集合相同的快照：占用集不变，仍自增版本 ----
    # 当前占用为空（v13）；恢复到 v12 快照（占用同样为空）。保留区
    # 保持当前状态不变（恢复只动已分配地址）。
    r = _restore(13, 12)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 14
    assert body["allocated_cidrs"] == []
    pool = _pool()
    assert pool["version"] == 14
    assert pool["allocated_cidrs"] == []
    assert pool["reserved_cidrs"] == [
        "10.10.16.0/25",
        "10.10.254.0/24",
    ]
    print("restore identical allocated set still bumps version ok")

    # ---- 非法请求体 -> 422，池与版本不变 ----
    before = _pool()
    for payload in (
        {"pool_version": 0, "snapshot_version": 1},
        {"pool_version": 1, "snapshot_version": 0},
        {"pool_version": -3, "snapshot_version": 1},
        {"pool_version": 1, "snapshot_version": -1},
        {"pool_version": 1},
        {"snapshot_version": 1},
        {},
    ):
        r = client.post("/api/allocations/restore", json=payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
    assert _pool() == before
    print("invalid request body -> 422, untouched ok")

    # ---- 同版本并发恢复：写锁互斥，恰有一个成功 ----
    # 先把保留区替换为空（当前占用为空，可以直接替换）。
    replaced = _replace(14, [])
    assert replaced["pool_version"] == 15
    barrier = threading.Barrier(2)
    results: dict[int, object] = {}

    def worker(idx, snapshot_version):
        barrier.wait()
        results[idx] = _restore(15, snapshot_version)

    t0 = threading.Thread(target=worker, args=(0, 4))
    t1 = threading.Thread(target=worker, args=(1, 2))
    t0.start()
    t1.start()
    t0.join(timeout=30)
    t1.join(timeout=30)
    assert not t0.is_alive() and not t1.is_alive()
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], codes
    winner = next(r for r in results.values() if r.status_code == 200)
    pool = _pool()
    assert pool["version"] == 16  # 只自增一次
    assert pool["allocated_cidrs"] == winner.json()["allocated_cidrs"]
    assert pool["reserved_cidrs"] == []
    print("concurrent restore: exactly one succeeds ok")

    print("ALL RESTORE TESTS PASSED")


if __name__ == "__main__":
    main()
