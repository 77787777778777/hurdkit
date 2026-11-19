#!/usr/bin/env python3
"""Drive the virtio benchmark matrix in the guest.

Reuses scripts/virtio_harness.py for SSH sessions, bounded timeouts, and
PASS/FAIL reporting. Emits smp-style JSON lines when BENCH_JSON=1.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import virtio_harness as vh

MODE = sys.argv[1] if len(sys.argv) > 1 else "all"
BENCH_RUNS = int(os.environ.get("BENCH_RUNS", "3"))
BENCH_JSON = os.environ.get("BENCH_JSON", "0") == "1"
RESULTS_DIR = os.environ.get("RESULTS_DIR", "/tmp")
TMP_DIR = os.environ.get("TMP_DIR", "/tmp")

VIRTIO_BLK_BIN = os.environ.get("VIRTIO_BLK_BIN", "")
VBLK_CLIENT_BIN = os.environ.get("VBLK_CLIENT_BIN", "")
VIRTIO_NET_BIN = os.environ.get("VIRTIO_NET_BIN", "")
VIRTIO_NET_CLIENT_BIN = os.environ.get("VIRTIO_NET_CLIENT_BIN", "")
VIRTIO_9P_BIN = os.environ.get("VIRTIO_9P_BIN", "")
NINEPFS_BIN = os.environ.get("NINEPFS_BIN", "")
VIRTIO_DIR = os.environ.get("VIRTIO_DIR", "")
VIRTIO_NET_DIR = os.environ.get("VIRTIO_NET_DIR", "")
BENCH_VBLK = os.environ.get("BENCH_VBLK", "")
BENCH_ORDINARY = os.environ.get("BENCH_ORDINARY", "")
BENCH_EXT2 = os.environ.get("BENCH_EXT2", "")
BENCH_SHARE = os.environ.get("BENCH_SHARE", "")
# The bench image stages the hurdkit-branch storeio (commit e07e9590, the
# --no-cache byte-to-block shift fix) at a separate path: the cross build
# has no parted, so it lacks the `part` store class and must not serve the
# system partition nodes.  /hurd/storeio stays Debian's.
STOREIO_BIN = os.environ.get("STOREIO_BIN", "/hurd/storeio-fixed")

B1_4K_BYTES = int(os.environ.get("B1_4K_BYTES", str(32 * 1024 * 1024)))
B1_1M_BYTES = int(os.environ.get("B1_1M_BYTES", str(128 * 1024 * 1024)))
B3_4K_BYTES = int(os.environ.get("B3_4K_BYTES", str(32 * 1024 * 1024)))
B3_1M_BYTES = int(os.environ.get("B3_1M_BYTES", str(64 * 1024 * 1024)))
N1_PINGS = int(os.environ.get("N1_PINGS", "20"))
N1_BYTES = int(os.environ.get("N1_BYTES", str(64 * 1024 * 1024)))
N1_CHUNK_BYTES = int(os.environ.get("N1_CHUNK_BYTES", str(64 * 1024)))

RUN_RECORDS = []


def progress(msg):
    # Announce a benchmark before it runs so long-running cases (large dd,
    # tar) are not silent.  Goes to stderr so it stays out of the stdout
    # JSON stream and the on-disk summary files.
    print(msg, file=sys.stderr, flush=True)


def emit_json(case, workload, backend, op, bs_bytes, nbytes, run, warmup,
              passed, elapsed_ms, rate, unit, command, notes=""):
    obj = {
        "case": case,
        "workload": workload,
        "backend": backend,
        "op": op,
        "bs_bytes": bs_bytes,
        "bytes": nbytes,
        "run": run,
        "warmup": warmup,
        "passed": bool(passed),
        "elapsed_ms": round(elapsed_ms, 3),
        "rate": round(rate, 3),
        "unit": unit,
        "command": command,
        "notes": notes,
    }
    RUN_RECORDS.append(obj)
    if BENCH_JSON:
        print(json.dumps(obj), flush=True)
    else:
        print(f"{'PASS' if passed else 'FAIL'}: {case} run={run} "
              f"{elapsed_ms:.1f} ms {rate:.2f} {unit}", flush=True)


def timed(session, command, timeout=vh.LONG_TIMEOUT):
    t0 = time.monotonic()
    status, out = session.run(command, timeout=timeout)
    dt = time.monotonic() - t0
    return status, out, dt


def bs_name(bs):
    if bs == 4096:
        return "4K"
    if bs == 1024 * 1024:
        return "1M"
    return str(bs)


def install_binaries(session):
    if VIRTIO_BLK_BIN and os.path.exists(VIRTIO_BLK_BIN):
        vh.install(VIRTIO_BLK_BIN, "/tmp/virtio-blk")
        session.run("install -m 755 /tmp/virtio-blk /hurd/virtio-blk", timeout=60)
    if VBLK_CLIENT_BIN and os.path.exists(VBLK_CLIENT_BIN):
        vh.install(VBLK_CLIENT_BIN, "/tmp/vblk-client")
        session.run("install -m 755 /tmp/vblk-client /hurd/vblk-client", timeout=60)
    if VIRTIO_NET_BIN and os.path.exists(VIRTIO_NET_BIN):
        vh.install(VIRTIO_NET_BIN, "/tmp/virtio-net")
        session.run("install -m 755 /tmp/virtio-net /hurd/virtio-net", timeout=60)
    if VIRTIO_NET_CLIENT_BIN and os.path.exists(VIRTIO_NET_CLIENT_BIN):
        vh.install(VIRTIO_NET_CLIENT_BIN, "/tmp/vnet-client")
        session.run("install -m 755 /tmp/vnet-client /hurd/vnet-client", timeout=60)
    if VIRTIO_9P_BIN and os.path.exists(VIRTIO_9P_BIN):
        vh.install(VIRTIO_9P_BIN, "/tmp/virtio-9p")
        session.run("install -m 755 /tmp/virtio-9p /hurd/virtio-9p", timeout=60)
    if NINEPFS_BIN and os.path.exists(NINEPFS_BIN):
        vh.install(NINEPFS_BIN, "/tmp/9pfs")
        session.run("install -m 755 /tmp/9pfs /hurd/9pfs", timeout=60)


def ensure_store_node(session, serve_node, store_node, device_arg=None):
    session.run(f"settrans -g {serve_node} 2>/dev/null || true", timeout=30)
    cmd = f"settrans -c {serve_node} /hurd/virtio-blk --serve"
    if device_arg:
        cmd += f" --device={device_arg}"
    session.run(cmd, timeout=120)
    session.run(f"settrans -g {store_node} 2>/dev/null || true", timeout=30)
    # storeio spec must use the full node path with a leading slash:
    # '@/dev/vblk0:' works, the slash-less form crashes storeio
    # (signal 6/11, /hurd/crash PCs in the serial log).
    session.run(f"settrans -c {store_node} {STOREIO_BIN} -T device '@{serve_node}:'",
               timeout=120)


def setup_virtio_blk(session):
    ensure_store_node(session, "/dev/vblk0", "/tmp/vblk-store")
    status, _ = session.run("test -e /tmp/vblk-store", timeout=30)
    vh.record("setup virtio-blk serve", status == 0, "")
    return "/tmp/vblk-store"


def setup_9p(session):
    tag = "benchfs"
    session.run("settrans -g /dev/virtio-9p0 2>/dev/null || true", timeout=30)
    session.run(f"settrans -c /dev/virtio-9p0 /hurd/virtio-9p --mount-tag={tag}",
               timeout=120)
    mnt = "/mnt/bench-9p"
    session.run(f"mkdir -p {mnt}", timeout=30)
    session.run(f"settrans -g {mnt} 2>/dev/null || true", timeout=30)
    session.run(f"settrans -c {mnt} /hurd/9pfs /dev/virtio-9p0", timeout=120)
    return mnt


def setup_ext2(session):
    mnt = "/mnt/bench-ext2"
    session.run(f"mkdir -p {mnt}", timeout=30)
    # Use a second virtio-blk device (PCI 0x9) served via storeio.
    ensure_store_node(session, "/dev/vblk1", "/tmp/ext2-store", "0000:00:09.0")
    session.run(f"settrans -g {mnt} 2>/dev/null || true", timeout=30)
    status, out = session.run(f"settrans -c {mnt} /hurd/ext2fs /tmp/ext2-store",
                             timeout=120)
    if status == 0:
        vh.record("setup ext2 mount", True, "node=/tmp/ext2-store")
        return mnt, "/tmp/ext2-store"
    vh.record("setup ext2 mount", False, out.strip()[-200:])
    return mnt, None


def _ssh_transfer(op, nbytes, chunk):
    """Stream nbytes through the control SSH connection (127.0.0.1 to the
    guest).  "tx" runs dd in the guest and drains the stream on the host;
    "rx" pipes zeros from the host into the guest's cat.  Both network
    legs ride the NIC under test the same way, so SSH's crypto overhead
    is identical across backends and the comparison stays fair.  Returns
    (bytes actually transferred, elapsed seconds, error detail or None)."""
    count = nbytes // chunk
    cmd = ["ssh", "-o", "BatchMode=yes", "-i", vh.SSH_KEY,
           "-p", str(vh.SSH_PORT), f"root@{vh.SSH_HOST}"]
    if op == "tx":
        cmd.append(f"dd if=/dev/zero bs={chunk} count={count} 2>/dev/null")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        t0 = time.monotonic()
        got = len(proc.stdout.read())
        err = proc.stderr.read().decode(errors="replace").strip()
        dt = time.monotonic() - t0
        rc = proc.wait()
        detail = None if rc == 0 else f"ssh rc={rc} err={err!r}"
    else:
        cmd.append("cat > /dev/null")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        payload = bytes(chunk)
        got = 0
        t0 = time.monotonic()
        detail = None
        for _ in range(count):
            try:
                proc.stdin.write(payload)
                got += chunk
            except BrokenPipeError:
                detail = "broken pipe writing to ssh stdin"
                break
        try:
            proc.stdin.close()
        except BrokenPipeError:
            if detail is None:
                detail = "broken pipe closing ssh stdin"
        dt = time.monotonic() - t0
        rc = proc.wait()
        err = proc.stderr.read().decode(errors="replace").strip()
        if detail is None and rc != 0:
            detail = f"ssh rc={rc} err={err!r}"
    return got, dt, detail


def run_net_throughput(session, backend):
    for op in ("tx", "rx"):
        case = f"N1-{backend}-{op}-{bs_name(N1_CHUNK_BYTES)}"
        progress(f"virtio-bench: applying {case}")
        # warm-up: a small transfer so the first timed run doesn't pay
        # the connection setup
        _ssh_transfer(op, min(N1_BYTES, 1024 * 1024), N1_CHUNK_BYTES)
        for run in range(1, BENCH_RUNS + 1):
            got, dt, detail = _ssh_transfer(op, N1_BYTES, N1_CHUNK_BYTES)
            passed = got == N1_BYTES and dt > 0
            rate = (N1_BYTES / (1024 * 1024)) / dt if dt > 0 else 0.0
            notes = f"{got} bytes in {dt:.2f}s"
            if detail:
                notes += f"; {detail}"
            if not passed:
                progress(f"virtio-bench: {case} run={run} got={got} "
                         f"expected={N1_BYTES} dt={dt:.2f}s detail={detail}")
            emit_json(case, "N1", backend, op, N1_CHUNK_BYTES, N1_BYTES,
                      run, False, passed, dt * 1000.0, rate, "MiB/s",
                      f"dd bs={N1_CHUNK_BYTES} over ssh ({op})",
                      notes)


def run_net_ping(session, case, backend):
    progress(f"virtio-bench: applying {case}")
    t0 = time.monotonic()
    status, out = session.run(f"ping -c {N1_PINGS} -i 0.1 10.0.2.2",
                              timeout=vh.LONG_TIMEOUT)
    dt = time.monotonic() - t0
    emit_json(case, "N1", backend, "latency", 0, N1_PINGS, 1, False,
              status == 0, dt * 1000.0,
              (N1_PINGS / dt) if dt > 0 else 0.0, "pps",
              f"ping -c {N1_PINGS} -i 0.1 10.0.2.2",
              out.strip().splitlines()[-1] if out.strip() else "")


def run_net_e1000(session):
    # e1000 leg: the guest's only NIC is the stock e1000, brought up at
    # boot by the staged interfaces (DHCP); ping and SSH both ride it.
    run_net_ping(session, "N1-e1000-latency", "e1000")
    run_net_throughput(session, "e1000")


def run_net_vnet(session):
    # virtio-net leg: the bench-vnet image stages the driver and an
    # `auto /dev/vnet0` interfaces entry, so the translator is already
    # serving and DHCP has run before SSH answers; no pfinet setup here.
    run_net_ping(session, "N1-virtio-net-latency", "virtio-net")
    run_net_throughput(session, "virtio-net")
    # The driver's --selftest must NOT run here: it would be a second
    # virtio-net instance over the live device -- it re-registers the IRQ
    # delivery port and reprograms the queues, and the serving
    # translator's stack (pfinet) then aborts.  The selftest belongs to
    # the dedicated harness (scripts/virtio_net_test.sh), which runs it
    # on a device nobody else serves.


def dd_command(target, bs, count, write):
    if write:
        return f"dd if=/dev/zero of={target} bs={bs} count={count} 2>&1"
    return f"dd if={target} of=/dev/null bs={bs} count={count} 2>&1"


def run_dd_case(session, case, workload, backend, target, bs, nbytes, write,
                cleanup=True):
    count = nbytes // bs
    cmd = dd_command(target, bs, count, write)
    progress(f"virtio-bench: applying {case}")
    # warmup
    status, out, dt = timed(session, cmd)
    passed = (status == 0)
    m = re.search(r"(\d+) bytes", out)
    if m and int(m.group(1)) != nbytes:
        passed = False
    rate = (nbytes / (1024 * 1024)) / dt if dt > 0 else 0.0
    emit_json(case, workload, backend, "write" if write else "read",
              bs, nbytes, 0, True, passed, dt * 1000.0, rate, "MiB/s", cmd,
              out.strip().splitlines()[-1] if out.strip() else "")
    if write and cleanup:
        # Only file targets (B3) are removed; a raw store node (B1) must
        # survive: rm would unlink the translator node the reads need.
        session.run(f"rm -f {target}", timeout=60)
    for run in range(1, BENCH_RUNS + 1):
        status, out, dt = timed(session, cmd)
        passed = (status == 0)
        m = re.search(r"(\d+) bytes", out)
        if m and int(m.group(1)) != nbytes:
            passed = False
        rate = (nbytes / (1024 * 1024)) / dt if dt > 0 else 0.0
        emit_json(case, workload, backend, "write" if write else "read",
                  bs, nbytes, run, False, passed, dt * 1000.0, rate, "MiB/s", cmd,
                  out.strip().splitlines()[-1] if out.strip() else "")
        if write and cleanup:
            session.run(f"rm -f {target}", timeout=60)
    return


def boot_device_prefix(session):
    # The root store spec names the boot disk, e.g. "part:2:device:wd0";
    # B1's ordinary cases write raw zeros, so the boot disk's name is
    # needed to keep them off it.
    status, out = session.run("fsysopts /", timeout=30)
    m = re.search(r"device:([a-z]+)(\d+)", out)
    if status == 0 and m:
        return m.group(1), int(m.group(2))
    return None, None


def probe_ordinary_device(session, candidates):
    # Kernel disk names differ between gnumach builds (wd* on hurdkit,
    # hd* on Debian) and the /dev entries don't track them, so candidates
    # are probed with a real open through a throwaway storeio node: the
    # first device whose 1-block read succeeds is the bench disk.
    for name in candidates:
        node = f"/tmp/ordinary-probe-{name}"
        session.run(f"settrans -g {node} 2>/dev/null; rm -f {node}", timeout=30)
        session.run(f"settrans -c {node} /hurd/storeio -T device {name}",
                    timeout=60)
        status, _ = session.run(
            f"dd if={node} of=/dev/null bs=512 count=1 2>/dev/null", timeout=60)
        session.run(f"settrans -g {node} 2>/dev/null; rm -f {node}", timeout=30)
        if status == 0:
            return name
    return None


def ordinary_node_setup(session):
    base, num = boot_device_prefix(session)
    base = base or "wd"
    num = num if num is not None else 0
    # The bench disk is the second disk the kernel enumerated: same
    # prefix, next unit, with the other naming scheme as fallback.
    candidates = []
    for name in (f"{base}{num + 1}", f"{base}{num + 2}", "hd1", "hd2", "wd1"):
        if name != f"{base}{num}" and name not in candidates:
            candidates.append(name)
    device = probe_ordinary_device(session, candidates)
    if device is None:
        vh.record("ordinary device probe", False,
                  f"no candidate of {candidates} opened")
        return None
    node = "/tmp/ordinary-store"
    session.run(f"settrans -g {node} 2>/dev/null; rm -f {node}", timeout=30)
    status, _ = session.run(
        f"settrans -c {node} /hurd/storeio --writable -T device {device}",
        timeout=60)
    vh.record("ordinary node selected", status == 0, f"{node} (device {device})")
    return node if status == 0 else None


def run_block(session):
    progress("virtio-bench: applying B1 block tests")
    install_binaries(session)
    vblk_node = setup_virtio_blk(session)
    ordinary_node = ordinary_node_setup(session)

    for bs, nbytes in ((4096, B1_4K_BYTES), (1024 * 1024, B1_1M_BYTES)):
        run_dd_case(session, f"B1-virtio-blk-write-{bs_name(bs)}", "B1",
                    "virtio-blk", vblk_node, bs, nbytes, True, cleanup=False)
        run_dd_case(session, f"B1-virtio-blk-read-{bs_name(bs)}", "B1",
                    "virtio-blk", vblk_node, bs, nbytes, False, cleanup=False)
        if ordinary_node is None:
            continue
        run_dd_case(session, f"B1-ordinary-write-{bs_name(bs)}", "B1",
                    "ordinary-blk", ordinary_node, bs, nbytes, True, cleanup=False)
        run_dd_case(session, f"B1-ordinary-read-{bs_name(bs)}", "B1",
                    "ordinary-blk", ordinary_node, bs, nbytes, False, cleanup=False)


def run_fs_dd(session):
    progress("virtio-bench: applying B3 filesystem dd tests")
    install_binaries(session)
    mnt9 = setup_9p(session)
    mnt_ext, ext_node = setup_ext2(session)
    for bs, nbytes in ((4096, B3_4K_BYTES), (1024 * 1024, B3_1M_BYTES)):
        for backend, mnt in (("ext2", mnt_ext), ("virtio-9pfs", mnt9)):
            if mnt is None:
                vh.record(f"skip {backend} (mount unavailable)", False, "")
                continue
            target = f"{mnt}/bench-dd"
            # write
            run_dd_case(session, f"B3-{backend}-write-{bs_name(bs)}", "B3",
                        backend, target, bs, nbytes, True)
            # read (file exists from last write iteration's final? ensure create)
            session.run(f"dd if=/dev/zero of={target} bs={bs} count={nbytes//bs}",
                       timeout=vh.LONG_TIMEOUT)
            run_dd_case(session, f"B3-{backend}-read-{bs_name(bs)}", "B3",
                        backend, target, bs, nbytes, False)
            session.run(f"rm -f {target}", timeout=60)


def run_fs_tar(session):
    progress("virtio-bench: applying B2 filesystem tar tests")
    install_binaries(session)
    mnt9 = setup_9p(session)
    mnt_ext, ext_node = setup_ext2(session)
    # Generate corpus on /tmp then tar; simplified: create 1000 small files.
    corpus = "/tmp/bench-corpus"
    session.run(f"rm -rf {corpus} && mkdir -p {corpus}", timeout=60)
    # Guest commands go through the session's { ...; } wrapper, which
    # breaks heredoc terminators (the closing line never matches), so the
    # generator is piped in base64-encoded instead.
    corpus_py = (
        "import os, random\n"
        "random.seed(42)\n"
        "root = '/tmp/bench-corpus'\n"
        "for i in range(1000):\n"
        "    d = os.path.join(root, 'd%d' % (i % 10), 's%d' % (i % 5))\n"
        "    os.makedirs(d, exist_ok=True)\n"
        "    with open(os.path.join(d, 'f%d' % i), 'wb') as f:\n"
        "        f.write(os.urandom(random.randint(512, 65536)))\n"
    )
    b64 = base64.b64encode(corpus_py.encode()).decode()
    session.run(f"printf %s {b64} | base64 -d | python3", timeout=600)
    archive = "/tmp/bench.tar"
    for backend, mnt in (("ext2", mnt_ext), ("virtio-9pfs", mnt9)):
        if mnt is None:
            vh.record(f"skip {backend} tar (mount unavailable)", False, "")
            continue
        dest = f"{mnt}/extract"
        session.run(f"rm -rf {dest} && mkdir -p {dest}", timeout=60)
        # create
        t0 = time.monotonic()
        status, out = session.run(f"tar -cf {archive} -C {corpus} .", timeout=vh.LONG_TIMEOUT)
        # move archive to target? keep on /tmp; time create of tree copy instead
        dt = time.monotonic() - t0
        # We'll instead time copying corpus into target FS.
        # Re-time: create tar on target
        progress(f"virtio-bench: applying B2-{backend}-tar-create")
        t0 = time.monotonic()
        status, out = session.run(f"tar -cf {mnt}/bench.tar -C {corpus} .",
                                 timeout=vh.LONG_TIMEOUT)
        dt = time.monotonic() - t0
        emit_json(f"B2-{backend}-tar-create", "B2", backend, "create",
                  0, 0, 1, False, status == 0, dt * 1000.0, 0.0, "ms",
                  f"tar -cf {mnt}/bench.tar -C {corpus} .", out.strip()[-100:])
        # extract
        progress(f"virtio-bench: applying B2-{backend}-tar-extract")
        t0 = time.monotonic()
        status, out = session.run(f"rm -rf {dest} && mkdir -p {dest} && "
                                 f"tar --no-same-owner -xf {mnt}/bench.tar -C {dest}",
                                 timeout=vh.LONG_TIMEOUT)
        dt = time.monotonic() - t0
        emit_json(f"B2-{backend}-tar-extract", "B2", backend, "extract",
                  0, 0, 1, False, status == 0, dt * 1000.0, 0.0, "ms",
                  f"tar -xf {mnt}/bench.tar -C {dest}", out.strip()[-100:])


def run_net(session):
    progress("virtio-bench: applying N1 network tests")
    install_binaries(session)
    if MODE in ("net-e1000", "all"):
        # "all" runs only the e1000 leg in this boot; the virtio-net leg
        # gets its own boot on the bench-vnet image (one backend per boot).
        run_net_e1000(session)
    elif MODE == "net-vnet":
        run_net_vnet(session)
    else:
        # legacy single-guest fallback (not used by make virtio-bench)
        run_net_e1000(session)
        run_net_vnet(session)


def write_summary():
    payload = {"meta": {"mode": MODE, "bench_runs": BENCH_RUNS,
                        "bench_json": BENCH_JSON},
               "cases": RUN_RECORDS}
    # Mode-specific filename so concurrent/sequential legs (e.g. the two
    # boots of MODE=all) do not clobber each other's aggregates; the shell
    # merges them into virtio-bench-summary.json at the end of MODE=all.
    mode_tag = MODE.replace("/", "-")
    names = ["virtio-bench-summary.json",
             f"virtio-bench-summary-{mode_tag}.json"]
    for d in (RESULTS_DIR, TMP_DIR):
        try:
            os.makedirs(d, exist_ok=True)
            for name in names:
                path = os.path.join(d, name)
                with open(path, "w") as f:
                    json.dump(payload, f, indent=2)
            print(f"summary written: {os.path.join(d, names[0])}", flush=True)
        except Exception as exc:
            print(f"summary write failed ({d}): {exc}", flush=True)


def main():
    session = None
    failed_early = False
    try:
        session = vh.GuestSession()
        install_binaries(session)
        if MODE in ("block", "all"):
            try:
                run_block(session)
            except Exception as exc:
                vh.record("block mode failed", False, str(exc))
        if MODE in ("fs-dd", "all"):
            try:
                run_fs_dd(session)
            except Exception as exc:
                vh.record("fs-dd mode failed", False, str(exc))
        if MODE in ("fs-tar", "all"):
            try:
                run_fs_tar(session)
            except Exception as exc:
                vh.record("fs-tar mode failed", False, str(exc))
        if MODE in ("net", "all", "net-e1000", "net-vnet"):
            try:
                run_net(session)
            except Exception as exc:
                vh.record("net mode failed", False, str(exc))
    except vh.GuestError as exc:
        vh.record("harness", False, str(exc))
        failed_early = True
    if failed_early:
        try:
            vh.dump_commands(session, (("qemu log tail", "true"),))
        except Exception:
            pass
        print(vh.qemu_log_tail(), flush=True)
    write_summary()
    return vh.finish(session)


if __name__ == "__main__":
    sys.exit(main())
