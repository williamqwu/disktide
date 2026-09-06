/* One directory read plus the lstat of every non-directory entry inside a
 * single GIL release.
 *
 *     scan_dir(fd, stat_dirs=False)
 *         -> list[(name, d_type, errno, mode, size, blocks, dev, ino, nlink,
 *                  mtime)]
 *
 * `mode` through `mtime` are None when the entry was not statted (a
 * directory, unless `stat_dirs`) or when its stat failed, in which case
 * `errno` says why.  The caller keeps ownership of `fd`: we dup it for
 * fdopendir, exactly as os.scandir(fd) does, and stat relative to the
 * caller's descriptor.
 *
 * Why this exists: every DirEntry.stat releases and re-acquires the GIL, so
 * a scan of an 88,000-directory tree made ~976,000 handoffs and eight
 * workers finished *slower* than one.  Reading a whole directory in C makes
 * one handoff per directory instead of one per entry.
 *
 * Error reporting matches os.scandir's shape:
 *   - the open itself failing (dup/fdopendir) raises OSError, like
 *     os.scandir(fd) does;
 *   - a readdir that fails part way through a directory appends one final
 *     entry with an empty name and that errno, so the caller counts it the
 *     way it counted the OSError the scandir iterator used to raise -- one
 *     inaccessible-or-vanished entry, with everything read before it kept.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* d_type and the DT_* constants travel together: a <dirent.h> that has one
 * has the other.  Where they are absent every entry reads as DT_UNKNOWN and
 * the caller falls back to the stat mode, which is the same path it already
 * takes on a filesystem that returns DT_UNKNOWN. */
#ifndef DT_UNKNOWN
#  define DT_UNKNOWN 0
#  define DT_DIR 4
#  define SCANFAST_D_TYPE(de) ((unsigned char)DT_UNKNOWN)
#else
#  define SCANFAST_D_TYPE(de) ((unsigned char)(de)->d_type)
#endif

/* macOS spells the nanosecond field st_mtimespec; modern SDKs alias st_mtim
 * to it under __DARWIN_C_LEVEL >= 200809L, older ones do not. */
#if defined(__APPLE__) && !defined(st_mtim)
#  define SCANFAST_MTIM(st) ((st).st_mtimespec)
#else
#  define SCANFAST_MTIM(st) ((st).st_mtim)
#endif

typedef struct {
    char *name;
    unsigned char dtype;
    int err;
    int statted;
    struct stat st;
} entry_t;

static int
push_entry(entry_t **ents, size_t *n, size_t *cap, const char *name)
{
    if (*n == *cap) {
        size_t grown_cap = *cap ? *cap * 2 : 32;
        entry_t *grown = realloc(*ents, grown_cap * sizeof(entry_t));
        if (grown == NULL)
            return ENOMEM;
        *ents = grown;
        *cap = grown_cap;
    }
    entry_t *e = &(*ents)[(*n)];
    e->name = strdup(name);
    if (e->name == NULL)
        return ENOMEM;
    e->dtype = DT_UNKNOWN;
    e->err = 0;
    e->statted = 0;
    (*n)++;
    return 0;
}

static PyObject *scan_dir(PyObject *self, PyObject *args)
{
    (void)self;
    int fd, stat_dirs = 0;
    if (!PyArg_ParseTuple(args, "i|p:scan_dir", &fd, &stat_dirs))
        return NULL;
    entry_t *ents = NULL;
    size_t n = 0, cap = 0;
    int fail = 0;

    Py_BEGIN_ALLOW_THREADS
    int dfd = dup(fd);
    if (dfd < 0) {
        fail = errno;
    } else {
        DIR *d = fdopendir(dfd);
        if (d == NULL) {
            fail = errno;
            close(dfd);
        } else {
            struct dirent *de;
            errno = 0;
            while ((de = readdir(d)) != NULL) {
                const char *nm = de->d_name;
                if (nm[0] == '.' && (nm[1] == 0 || (nm[1] == '.' && nm[2] == 0))) {
                    errno = 0;
                    continue;
                }
                int oom = push_entry(&ents, &n, &cap, nm);
                if (oom) { fail = oom; break; }
                entry_t *e = &ents[n - 1];
                e->dtype = SCANFAST_D_TYPE(de);
                if (e->dtype != DT_DIR || stat_dirs) {
                    /* Relative to the caller's descriptor, not the dup:
                     * POSIX leaves any use of an fd handed to fdopendir
                     * unspecified, and the caller is blocked in this call
                     * for as long as we are here. */
                    if (fstatat(fd, nm, &e->st, AT_SYMLINK_NOFOLLOW) == 0)
                        e->statted = 1;
                    else
                        e->err = errno;
                }
                errno = 0;
            }
            /* readdir returns NULL both at the end of the directory (errno
             * untouched, and we zeroed it) and on a real failure. */
            if (!fail && errno != 0) {
                int read_err = errno;
                if (push_entry(&ents, &n, &cap, "") == 0)
                    ents[n - 1].err = read_err;
                else
                    fail = ENOMEM;
            }
            /* os.scandir(fd) rewinds before it lets the directory go, so the
             * descriptor it was handed can be read again; `dup` shares the
             * file offset, so without this the caller gets its descriptor
             * back at the end of the directory and a second read of it --
             * by either reader -- finds nothing. */
            rewinddir(d);
            closedir(d);
        }
    }
    Py_END_ALLOW_THREADS

    if (fail) {
        for (size_t i = 0; i < n; i++) free(ents[i].name);
        free(ents);
        errno = fail;
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    PyObject *list = PyList_New((Py_ssize_t)n);
    if (list == NULL) goto fail_all;
    for (size_t i = 0; i < n; i++) {
        entry_t *e = &ents[i];
        PyObject *name = PyUnicode_DecodeFSDefault(e->name);
        free(e->name);
        e->name = NULL;
        if (name == NULL) goto fail_all;
        PyObject *t;
        if (e->statted) {
            double mtime = (double)SCANFAST_MTIM(e->st).tv_sec
                         + 1e-9 * (double)SCANFAST_MTIM(e->st).tv_nsec;
            /* Signed for size and blocks (off_t, blkcnt_t), unsigned for the
             * identity triple, which is what the stat_result fields Python
             * builds from these carry. */
            t = Py_BuildValue("(NBiILLKKKd)", name, e->dtype, e->err,
                              (unsigned int)e->st.st_mode,
                              (long long)e->st.st_size,
                              (long long)e->st.st_blocks,
                              (unsigned long long)e->st.st_dev,
                              (unsigned long long)e->st.st_ino,
                              (unsigned long long)e->st.st_nlink, mtime);
        } else {
            t = Py_BuildValue("(NBiOOOOOOO)", name, e->dtype, e->err,
                              Py_None, Py_None, Py_None, Py_None, Py_None, Py_None, Py_None);
        }
        if (t == NULL) goto fail_all;
        PyList_SET_ITEM(list, (Py_ssize_t)i, t);
    }
    free(ents);
    return list;

fail_all:
    for (size_t i = 0; i < n; i++) free(ents[i].name);
    free(ents);
    Py_XDECREF(list);
    return NULL;
}

PyDoc_STRVAR(scan_dir_doc,
"scan_dir(fd, stat_dirs=False) -> list of tuples\n\n"
"Read the directory open on `fd` and lstat every entry that is not a\n"
"directory, in one GIL release.  Each tuple is\n"
"(name, d_type, errno, mode, size, blocks, dev, ino, nlink, mtime); the\n"
"stat fields are None when the entry was not statted or its stat failed.\n"
"`fd` stays open and owned by the caller.");

static PyMethodDef methods[] = {
    {"scan_dir", scan_dir, METH_VARARGS, scan_dir_doc},
    {NULL, NULL, 0, NULL}
};

PyDoc_STRVAR(module_doc,
"Batched directory reader for the disktide scanner.\n\n"
"Optional: `disktide.scanner.accel` falls back to `_scanfast_py` when this\n"
"module was not built for the running interpreter.");

static struct PyModuleDef mod = {
    PyModuleDef_HEAD_INIT, "disktide.scanner._scanfast", module_doc, -1,
    methods, NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC PyInit__scanfast(void) { return PyModule_Create(&mod); }
