#!/usr/bin/env python3
"""Minimal 9P2000.L file server for the cross-compile plan's V5 mount test.

Serves a host directory tree over 9P2000.L so the Hurd guest can mount it
via the cross-built 9pfs translator (10.0.2.2:1564 under QEMU user net).

Usage: ./9p-server.py [ROOT_DIR] [LISTEN_ADDR:PORT]
Defaults: ROOT_DIR=~/work/9p-root, listen on 127.0.0.1:564.

Intent: best-effort, one thread per connection, read+write support. Good
enough for create/read/write/stat/readdir of small files (the canary test).
Not a production 9P server — see stuff/crosscompile_plan.md §6 V5.

Message formats follow 9P2000.L as implemented by bugaevc/9pfs
(9p.h / 9p-rpc.c); the error path replies with a classic 9P Rerror
(ecode[4] + ename[2]s + errno[4]).
"""

import os
import signal
import socket
import stat
import struct
import sys
import threading
from pathlib import Path

# 9P2000.L message types (see 9p.h)
TSTATFS = 8
RSTATFS = 9
TLOPEN = 12
RLOPEN = 13
TLCREATE = 14
RLCREATE = 15
TSYMLINK = 16
RSYMLINK = 17
TREADLINK = 22
RREADLINK = 23
TGETATTR = 24
RGETATTR = 25
TSETATTR = 26
RSETATTR = 27
TREADDIRECTORY = 40
RREADDIRECTORY = 41

# Core 9P types (shared with 9P2000.L)
TVERSION = 100
RVERSION = 101
TATTACH = 104
RATTACH = 105
RERROR = 107
TFLUSH = 108
RFLUSH = 109
TWALK = 110
RWALK = 111
TREAD = 116
RREAD = 117
TWRITE = 118
RWRITE = 119
TCLUNK = 120
RCLUNK = 121
TREMOVE = 122
RREMOVE = 123
TLINK = 70
RLINK = 71
TMKDIR = 72
RMKDIR = 73
TRENAMEAT = 74
RRENAMEAT = 75
TUNLINKAT = 76
RUNLINKAT = 77

# QID types
QTDIR = 0x80
QTLNK = 0x02
QTFILE = 0x00

# getattr masks
GETATTR_MODE = 0x1
GETATTR_NLINK = 0x2
GETATTR_UID = 0x4
GETATTR_GID = 0x8
GETATTR_RDEV = 0x10
GETATTR_ATIME = 0x20
GETATTR_MTIME = 0x40
GETATTR_CTIME = 0x80
GETATTR_INO = 0x100
GETATTR_SIZE = 0x200
GETATTR_BLOCKS = 0x400
GETATTR_ALL = 0x7ff

# setattr masks
SETATTR_MODE = 0x1
SETATTR_UID = 0x2
SETATTR_GID = 0x4
SETATTR_SIZE = 0x8
SETATTR_ATIME = 0x10
SETATTR_MTIME = 0x20
SETATTR_ATIME_SET = 0x80
SETATTR_MTIME_SET = 0x100

# .L open flags (Linux)
O_WRONLY = 0x1
O_RDWR = 0x2
O_CREAT = 0x40
O_EXCL = 0x80
O_TRUNC = 0x200
O_APPEND = 0x400
O_DIRECTORY = 0x10000
O_NOFOLLOW = 0x20000
AT_REMOVEDIR = 0x200

NOFID = 0xffffffff
FID_ROOT = 0xffffffff

MSIZE = 8192
MAX_MSG = 1 << 20
VERSION = "9P2000.L"

DT_UNKNOWN = 0
DT_REG = 8
DT_DIR = 4
DT_LNK = 10

FS_TYPE_TMPFS = 0x01021994


def qid(st):
    if stat.S_ISDIR(st.st_mode):
        qtype = QTDIR
    elif stat.S_ISLNK(st.st_mode):
        qtype = QTLNK
    else:
        qtype = QTFILE
    return struct.pack("<B I Q", qtype, 0, st.st_ino & 0xFFFFFFFFFFFFFF)


def d_type(st):
    if stat.S_ISDIR(st.st_mode):
        return DT_DIR
    if stat.S_ISLNK(st.st_mode):
        return DT_LNK
    if stat.S_ISREG(st.st_mode):
        return DT_REG
    return DT_UNKNOWN


def pack_str(s):
    data = s.encode() if isinstance(s, str) else s
    return struct.pack("<H", len(data)) + data


class _DirHandle:
    """Placeholder for an opened directory; readdir is served from the path."""

    def seek(self, offset):
        return 0

    def read(self, count):
        return b""

    def write(self, data):
        raise OSError(21, "Is a directory")

    def flush(self):
        pass

    def close(self):
        pass


class Conn:
    def __init__(self, conn, root):
        self.conn = conn
        self.root = Path(root).resolve()
        self.fids = {}        # fid -> relative path (absolute Path under root)
        self.open_files = {}  # fid -> file object

    # ---------- framing ----------

    def send(self, mtype, tag, body):
        msg = struct.pack("<I B H", 4 + 1 + 2 + len(body), mtype, tag) + body
        self.conn.sendall(msg)

    def rerror(self, tag, ename, errno=2):
        # Classic 9P Rerror; the client (p9_version >= 2000.u) parses
        # ename + errno. Hurd's common errnos match Linux for these.
        eb = ename.encode()
        self.send(RERROR, tag, pack_str(eb) + struct.pack("<I", errno))

    def handle(self):
        buf = b""
        try:
            while True:
                while len(buf) < 4:
                    chunk = self.conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                (size,) = struct.unpack_from("<I", buf, 0)
                if size > MAX_MSG:
                    return
                while len(buf) < size:
                    chunk = self.conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                msg, buf = buf[:size], buf[size:]
                mtype = msg[4]
                (tag,) = struct.unpack_from("<H", msg, 5)
                body = msg[7:]
                if os.environ.get("P9_DEBUG"):
                    print("9p <- type=%d tag=%d len=%d" %
                          (mtype, tag, len(body)), flush=True)
                try:
                    handler = getattr(self, "t_%d" % mtype)
                except AttributeError:
                    self.rerror(tag, "unsupported 9P message %d" % mtype, 95)
                    continue
                try:
                    handler(tag, body)
                except BrokenPipeError:
                    return
                except Exception as exc:  # noqa: BLE001 - keep server alive
                    try:
                        self.rerror(tag, "server error: %s" % exc, 5)
                    except BrokenPipeError:
                        return
        except (ConnectionResetError, BrokenPipeError):
            return

    # ---------- helpers ----------

    def resolve(self, path):
        """Resolve *path* (absolute) and confine it to the root tree."""
        p = Path(path).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            return None
        return p

    def lookup_fid(self, fid):
        return self.fids.get(fid)

    def set_fid(self, fid, path):
        self.fids[fid] = path

    def parent_join(self, parent, name):
        if name in (".", ""):
            return parent
        if name == "..":
            if parent == self.root:
                return self.root
            return parent.parent
        p = parent / name
        p = self.resolve(p)
        return p

    # ---------- handlers ----------

    def t_100(self, tag, body):  # Tversion
        (msize,) = struct.unpack_from("<I", body, 0)
        (vlen,) = struct.unpack_from("<H", body, 4)
        version = body[6:6 + vlen].decode(errors="replace")
        if version != VERSION:
            # The client expects exactly this version for .L; the plan's
            # canary always offers 9P2000.L.
            self.rerror(tag, "unsupported 9P version %s" % version, 95)
            return
        msize = min(msize, MSIZE)
        self.send(RVERSION, tag,
                  struct.pack("<I", msize) + pack_str(VERSION))

    def t_104(self, tag, body):  # Tattach
        (fid, afid) = struct.unpack_from("<I I", body, 0)
        self.fids[fid] = self.root
        st = self.root.stat()
        self.send(RATTACH, tag, qid(st))

    def t_110(self, tag, body):  # Twalk
        (fid, newfid, nwnames) = struct.unpack_from("<I I H", body, 0)
        cur = self.fids.get(fid)
        if cur is None:
            self.rerror(tag, "fid not found", 2)
            return
        p = 10
        names = []
        for _ in range(nwnames):
            (n,) = struct.unpack_from("<H", body, p)
            p += 2
            names.append(body[p:p + n].decode(errors="replace"))
            p += n
        cur_path = cur
        qids = []
        # 9P mandates a partial walk: return the qids of the resolvable
        # prefix so the client can handle O_CREAT on the failing component.
        for name in names:
            nxt = self.parent_join(cur_path, name)
            if nxt is None or not nxt.exists():
                break
            qids.append(qid(nxt.lstat()))
            cur_path = nxt
        if not names:
            # Fid clone.
            self.fids[newfid] = cur
            self.send(RWALK, tag, struct.pack("<H", 0))
            return
        if len(qids) == 0:
            self.rerror(tag, "no such file or directory", 2)
            return
        self.fids[newfid] = cur_path
        self.send(RWALK, tag, struct.pack("<H", len(qids)) + b"".join(qids))

    def t_108(self, tag, body):  # Tflush
        self.send(RFLUSH, tag, b"")

    def t_116(self, tag, body):  # Tread
        (fid, offset, count) = struct.unpack_from("<I Q I", body, 0)
        f = self.open_files.get(fid)
        if f is None:
            self.rerror(tag, "fid not open", 9)
            return
        try:
            f.seek(offset)
            data = f.read(count)
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RREAD, tag, struct.pack("<I", len(data)) + data)

    def t_118(self, tag, body):  # Twrite
        (fid, offset, count) = struct.unpack_from("<I Q I", body, 0)
        data = body[16:16 + count]
        f = self.open_files.get(fid)
        if f is None:
            self.rerror(tag, "fid not open", 9)
            return
        try:
            f.seek(offset)
            n = f.write(data)
            f.flush()
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RWRITE, tag, struct.pack("<I", n))

    def t_120(self, tag, body):  # Tclunk
        (fid,) = struct.unpack_from("<I", body, 0)
        f = self.open_files.pop(fid, None)
        if f is not None:
            f.close()
        self.fids.pop(fid, None)
        self.send(RCLUNK, tag, b"")

    def t_122(self, tag, body):  # Tremove (classic; unused in .L)
        (fid,) = struct.unpack_from("<I", body, 0)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.fids.pop(fid, None)
        self.send(RREMOVE, tag, b"")

    # ---------- 9P2000.L ----------

    def t_8(self, tag, body):  # Tstatfs
        (fid,) = struct.unpack_from("<I", body, 0)
        st = self.root.stat()
        bsize = 4096
        body = struct.pack("<I I", FS_TYPE_TMPFS, bsize)
        body += struct.pack("<Q Q Q", 1 << 20, 1 << 19, 1 << 19)
        body += struct.pack("<Q Q", 8192, 4096)
        body += struct.pack("<Q I", 0, 255)
        self.send(RSTATFS, tag, body)

    def t_12(self, tag, body):  # Tlopen
        (fid, flags) = struct.unpack_from("<I I", body, 0)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        accmode = flags & 3
        if path.is_dir():
            # Directories are opened read-only for readdir purposes.
            pyflags = os.O_RDONLY | O_DIRECTORY
        else:
            pyflags = {0: os.O_RDONLY, 1: os.O_WRONLY, 2: os.O_RDWR}[accmode]
            if flags & O_CREAT:
                pyflags |= os.O_CREAT
            if flags & O_EXCL:
                pyflags |= os.O_EXCL
            if flags & O_TRUNC:
                pyflags |= os.O_TRUNC
            if flags & O_APPEND:
                pyflags |= os.O_APPEND
            if flags & O_NOFOLLOW:
                pyflags |= os.O_NOFOLLOW
        try:
            if path.is_dir():
                f = _DirHandle()
            else:
                fd = os.open(path, pyflags)
                f = os.fdopen(fd, "rb" if accmode == 0 else
                              ("wb" if accmode == 1 else "r+b"))
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.open_files[fid] = f
        st = path.stat()
        self.send(RLOPEN, tag, qid(st) + struct.pack("<I", 0))

    def t_14(self, tag, body):  # Tlcreate
        (fid,) = struct.unpack_from("<I", body, 0)
        p = 4
        (nlen,) = struct.unpack_from("<H", body, p)
        p += 2
        name = body[p:p + nlen].decode(errors="replace")
        p += nlen
        (flags, mode, gid) = struct.unpack_from("<I I I", body, p)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        target = self.parent_join(path, name)
        if target is None:
            self.rerror(tag, "invalid name", 22)
            return
        try:
            accmode = flags & 3
            pyflags = {0: os.O_RDONLY, 1: os.O_WRONLY, 2: os.O_RDWR}[accmode]
            pyflags |= os.O_CREAT
            if flags & O_TRUNC:
                pyflags |= os.O_TRUNC
            if flags & O_APPEND:
                pyflags |= os.O_APPEND
            if flags & O_EXCL:
                pyflags |= os.O_EXCL
            fd = os.open(target, pyflags, mode & 0o7777)
            f = os.fdopen(fd, "rb" if accmode == 0 else
                          ("wb" if accmode == 1 else "r+b"))
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        # The fid is rebound to the new file, as Tlcreate mandates.
        self.fids[fid] = target
        self.open_files[fid] = f
        st = target.stat()
        self.send(RLCREATE, tag, qid(st) + struct.pack("<I", 0))

    def t_16(self, tag, body):  # Tsymlink
        (fid,) = struct.unpack_from("<I", body, 0)
        p = 4
        (nlen,) = struct.unpack_from("<H", body, p)
        p += 2
        name = body[p:p + nlen].decode(errors="replace")
        p += nlen
        (tlen,) = struct.unpack_from("<H", body, p)
        p += 2
        target = body[p:p + tlen].decode(errors="replace")
        p += tlen
        (gid,) = struct.unpack_from("<I", body, p)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        link = self.parent_join(path, name)
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RSYMLINK, tag, qid(link.lstat()))

    def t_22(self, tag, body):  # Treadlink
        (fid,) = struct.unpack_from("<I", body, 0)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        try:
            target = os.readlink(path)
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RREADLINK, tag, pack_str(target))

    def t_24(self, tag, body):  # Tgetattr
        (fid, valid) = struct.unpack_from("<I Q", body, 0)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        st = path.lstat()
        ret_valid = valid & GETATTR_ALL
        out = struct.pack("<Q", ret_valid)
        out += qid(st)
        out += struct.pack("<I I I", st.st_mode, st.st_uid, st.st_gid)
        out += struct.pack("<Q", st.st_nlink)
        out += struct.pack("<Q", st.st_rdev)
        out += struct.pack("<Q", st.st_size)
        out += struct.pack("<Q", st.st_blksize)
        out += struct.pack("<Q", st.st_blocks)
        out += struct.pack("<Q Q", int(st.st_atime), 0)
        out += struct.pack("<Q Q", int(st.st_mtime), 0)
        out += struct.pack("<Q Q", int(st.st_ctime), 0)
        out += struct.pack("<Q Q", int(st.st_ctime), 0)
        out += struct.pack("<Q", 0)  # gen
        out += struct.pack("<Q", 0)  # data_version
        self.send(RGETATTR, tag, out)

    def t_26(self, tag, body):  # Tsetattr
        (fid, valid) = struct.unpack_from("<I I", body, 0)
        p = 8
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        try:
            if valid & SETATTR_MODE:
                (mode,) = struct.unpack_from("<I", body, p); p += 4
                os.chmod(path, mode & 0o7777)
            if valid & SETATTR_UID:
                (uid,) = struct.unpack_from("<I", body, p); p += 4
                try:
                    os.chown(path, uid, -1)
                except OSError:
                    pass
            if valid & SETATTR_GID:
                (gid,) = struct.unpack_from("<I", body, p); p += 4
                try:
                    os.chown(path, -1, gid)
                except OSError:
                    pass
            if valid & SETATTR_SIZE:
                (size,) = struct.unpack_from("<Q", body, p); p += 8
                with open(path, "r+b") as f:
                    f.truncate(size)
            atime = mtime = None
            if valid & SETATTR_ATIME:
                (atime, _atime_nsec) = struct.unpack_from("<Q Q", body, p); p += 16
            if valid & SETATTR_MTIME:
                (mtime, _mtime_nsec) = struct.unpack_from("<Q Q", body, p); p += 16
            if atime is not None or mtime is not None:
                st = path.stat()
                os.utime(path, (atime if atime is not None else st.st_atime,
                                mtime if mtime is not None else st.st_mtime))
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RSETATTR, tag, b"")

    def t_40(self, tag, body):  # Tlreaddir
        (fid, offset, count) = struct.unpack_from("<I Q I", body, 0)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        if not path.is_dir():
            self.rerror(tag, "not a directory", 20)
            return
        names = sorted(os.listdir(path))
        out = []
        total = 0
        for index, name in enumerate(names):
            if index < offset:
                continue
            entry_path = path / name
            try:
                st = entry_path.lstat()
            except OSError:
                continue
            entry = qid(st)
            entry += struct.pack("<Q", index + 1)  # next offset (cookie)
            entry += struct.pack("<B", d_type(st))
            nb = name.encode()
            entry += struct.pack("<H", len(nb)) + nb
            if total + len(entry) > count:
                break
            out.append(entry)
            total += len(entry)
        payload = b"".join(out)
        self.send(RREADDIRECTORY, tag,
                  struct.pack("<I", len(payload)) + payload)

    def t_72(self, tag, body):  # Tmkdir
        (fid,) = struct.unpack_from("<I", body, 0)
        p = 4
        (nlen,) = struct.unpack_from("<H", body, p)
        p += 2
        name = body[p:p + nlen].decode(errors="replace")
        p += nlen
        (mode, gid) = struct.unpack_from("<I I", body, p)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        target = self.parent_join(path, name)
        try:
            os.mkdir(target, mode & 0o7777)
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RMKDIR, tag, qid(target.stat()))

    def t_74(self, tag, body):  # Trenameat
        (oldfid,) = struct.unpack_from("<I", body, 0)
        p = 4
        (nlen,) = struct.unpack_from("<H", body, p)
        p += 2
        oldname = body[p:p + nlen].decode(errors="replace")
        p += nlen
        (newfid,) = struct.unpack_from("<I", body, p)
        p += 4
        (nlen2,) = struct.unpack_from("<H", body, p)
        p += 2
        newname = body[p:p + nlen2].decode(errors="replace")
        oldparent = self.fids.get(oldfid)
        newparent = self.fids.get(newfid)
        if oldparent is None or newparent is None:
            self.rerror(tag, "fid not found", 2)
            return
        try:
            os.rename(oldparent / oldname, newparent / newname)
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RRENAMEAT, tag, b"")

    def t_76(self, tag, body):  # Tunlinkat
        (fid,) = struct.unpack_from("<I", body, 0)
        p = 4
        (nlen,) = struct.unpack_from("<H", body, p)
        p += 2
        name = body[p:p + nlen].decode(errors="replace")
        p += nlen
        (flags,) = struct.unpack_from("<I", body, p)
        path = self.fids.get(fid)
        if path is None:
            self.rerror(tag, "fid not found", 2)
            return
        target = self.parent_join(path, name)
        try:
            if flags & AT_REMOVEDIR:
                target.rmdir()
            else:
                target.unlink()
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RUNLINKAT, tag, b"")

    def t_70(self, tag, body):  # Tlink
        (fid, dfid) = struct.unpack_from("<I I", body, 0)
        p = 8
        (nlen,) = struct.unpack_from("<H", body, p)
        p += 2
        name = body[p:p + nlen].decode(errors="replace")
        src = self.fids.get(fid)
        dstdir = self.fids.get(dfid)
        if src is None or dstdir is None:
            self.rerror(tag, "fid not found", 2)
            return
        try:
            os.link(src, dstdir / name)
        except OSError as exc:
            self.rerror(tag, str(exc), exc.errno or 5)
            return
        self.send(RLINK, tag, b"")


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/work/9p-root")
    listen = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1:564"
    Path(root).mkdir(parents=True, exist_ok=True)
    host, port = listen.rsplit(":", 1)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, int(port)))
    srv.listen(16)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    print("9p-server: serving %s on %s:%s" % (root, host, port), flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=Conn(conn, root).handle, daemon=True).start()


if __name__ == "__main__":
    main()
