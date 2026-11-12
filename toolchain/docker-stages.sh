#!/bin/sh
# Run inside the hurd-x86_64-gnu container with /src/3p (ro) and
# /opt/x86_64-gnu (rw) bind-mounted. Builds stages 1–6 (binutils → gcc2)
# for target x86_64-gnu, installing into the host-mounted prefix.
# MIG is a host tool built from the 3P/mig checkout (it is not copied from
# a VM and not downloaded at run time).
set -eu

PREFIX=/opt/x86_64-gnu
SRC=/src/3p
# Sources baked into the image; the build tree lives under $HOME so the
# container can run as the host user (no root-owned files in the prefix).
SRCDIR=/build/src
BUILD=${BUILD:-$HOME/build}
GCC_SRC=$SRCDIR/gcc-15.3.0
BINUTILS_SRC=$SRCDIR/binutils-2.46.1
NPROC=$(nproc)
export PATH=$PREFIX/bin:$PATH

mkdir -p "$HOME" "$BUILD"

echo "=== stage 1: binutils ==="
if [ ! -x "$PREFIX/bin/x86_64-gnu-as" ]; then
  rm -rf "$BUILD/binutils"
  mkdir -p "$BUILD/binutils"
  cd "$BUILD/binutils"
  "$BINUTILS_SRC/configure" --target=x86_64-gnu \
      --prefix="$PREFIX" --disable-werror --disable-nls
  make -j"$NPROC"
  make install
  rm -rf "$BUILD/binutils"
else
  echo "(skipping: x86_64-gnu-as already installed)"
fi

echo "=== stage 2: gcc stage 1 ==="
# The stage-1 compiler only has to produce libgcc for the target, so it is
# built single-threaded (--disable-threads selects gthr-single; the default
# posix threading needs pthread.h, which does not exist yet). Libgcc has
# to be built and installed before glibc: its loader links with -lgcc.
if [ ! -x "$PREFIX/bin/x86_64-gnu-gcc" ] || ! ls "$PREFIX"/lib/gcc/x86_64-gnu/*/libgcc.a >/dev/null 2>&1; then
  rm -rf "$BUILD/gcc-1"
  mkdir -p "$BUILD/gcc-1"
  cd "$BUILD/gcc-1"
  PATH="$PREFIX/bin:$PATH" "$GCC_SRC/configure" --target=x86_64-gnu \
      --prefix="$PREFIX" --enable-languages=c --without-headers \
      --disable-shared --disable-multilib --disable-nls --disable-threads
  PATH="$PREFIX/bin:$PATH" make all-gcc -j"$NPROC"
  make install-gcc
  PATH="$PREFIX/bin:$PATH" make all-target-libgcc -j"$NPROC"
  make install-target-libgcc
  rm -rf "$BUILD/gcc-1"
else
  echo "(skipping: x86_64-gnu-gcc already installed)"
fi

echo "=== stage 3: gnumach headers ==="
# gnumach has no generated configure; autoreconf writes to the source tree,
# which is read-only (bind-mounted). Copy to the build dir, autoreconf there,
# then build out-of-tree.
rm -rf "$BUILD/gnumach-src" "$BUILD/gnumach"
cp -a "$SRC/gnumach" "$BUILD/gnumach-src"
cd "$BUILD/gnumach-src" && autoreconf -i
mkdir -p "$BUILD/gnumach"
cd "$BUILD/gnumach"
"$BUILD/gnumach-src/configure" --prefix="$PREFIX" --host=x86_64-gnu
make install-data
rm -rf "$BUILD/gnumach-src" "$BUILD/gnumach"

echo "=== stage 4: MIG ==="
# MIG is a host tool (runs on x86_64-linux, emits target-independent C).
# It is built from the 3P/mig checkout (MIG 1.8+), which registers the basic
# C types the Hurd .defs files refer to; the released 1.3.1 tarball does not.
# The checkout ships no generated configure script and autoreconf writes into
# the source tree, so build from a copy. MIG is configured with
# --target=x86_64-gnu so that the .defs files are preprocessed by the target
# compiler; the Mach headers it needs come from stage 3.
rm -rf "$BUILD/mig-src" "$BUILD/mig"
cp -a "$SRC/mig" "$BUILD/mig-src"
cd "$BUILD/mig-src" && autoreconf -i
mkdir -p "$BUILD/mig"
cd "$BUILD/mig"
PATH="$PREFIX/bin:$PATH" "$BUILD/mig-src/configure" --prefix="$PREFIX" \
    --target=x86_64-gnu --host=x86_64-pc-linux-gnu --build=x86_64-pc-linux-gnu \
    --program-prefix= TARGET_CPPFLAGS="-I$PREFIX/include"
make -j"$NPROC"
make install
rm -rf "$BUILD/mig-src" "$BUILD/mig"

echo "=== stage 4b: Hurd headers ==="
# glibc compiles the Hurd .defs files (hurd/io.defs, ...) into RPC stubs
# before the Hurd libraries themselves are built; install the public
# headers and .defs into the prefix so both builds can find them.
mkdir -p "$PREFIX/include/hurd"
cp -f "$SRC/hurd/hurd/"*.defs "$SRC/hurd/hurd/"*.h "$PREFIX/include/hurd/"

echo "=== stage 5: glibc ==="
rm -rf "$BUILD/glibc"
mkdir -p "$BUILD/glibc"
cd "$BUILD/glibc"
# glibc's Hurd port needs hurd/version.h from the Hurd source tree at
# configure time (3p/hurd/hurd/version.h). The source tree is read-only, so
# add its include path via CPPFLAGS instead of installing headers first.
# The extra include directories come last (-idirafter): glibc ships its own
# copies of some Mach headers, and the gnumach ones would otherwise shadow
# them (both define round_page and trunc_page).
# UNIXOID_TRAPS is referenced by gnumach's legacy mach/syscall_sw.h but never
# defined; glibc builds with -Wundef -Werror, so define it away.
# The minimal gcc stage is C-only; glibc's configure would otherwise pick
# up the host g++ and try to build a C++ variant of support/links-dso-program
# (linking with -lstdc++ fails — there is no target C++ runtime yet).
# Give configure a stub C++ compiler: its "whether $CXX can link" check
# then fails, glibc clears CXX itself and selects the C variant (this is
# glibc's own documented fallback for a missing/unusable C++ compiler).
CPPFLAGS="-idirafter /src/3p/hurd -idirafter $PREFIX/include -DUNIXOID_TRAPS=0" \
CPATH="$PREFIX/include" CXX=false "$SRC/glibc/configure" \
    --host=x86_64-gnu --build=x86_64-pc-linux-gnu --prefix="$PREFIX" \
    --enable-obsolete-rpc --disable-multilib
make -j"$NPROC" MIG="$PREFIX/bin/mig -I$PREFIX/include"
make install MIG="$PREFIX/bin/mig -I$PREFIX/include"
rm -rf "$BUILD/glibc"

echo "=== stage 6: gcc stage 2 ==="
rm -rf "$BUILD/gcc-2"
mkdir -p "$BUILD/gcc-2"
cd "$BUILD/gcc-2"
"$GCC_SRC/configure" --target=x86_64-gnu \
    --prefix="$PREFIX" --enable-languages=c,c++ \
    --enable-threads=posix --disable-multilib
make -j"$NPROC"
make install
rm -rf "$BUILD/gcc-2"

echo
echo "=== stages 1–6 complete ==="
echo "MIG is installed at $PREFIX/bin/mig (built natively, not copied from VM)."
