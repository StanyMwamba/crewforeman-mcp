/* Tiny test-only launcher: run the fake dsh Python script with argv intact.
 * Windows CreateProcess cannot execute a shebang script, so unit tests and CI
 * invoke this PE instead.  Paths are baked in by tests/build_fake_dsh.py.
 *
 * Windows note: the MSVC _spawnv family joins argv with spaces without
 * quoting, so a multi-line / multi-word task reaches the child split into
 * words (a 3.13.3 CI failure).  Build a correctly quoted command line and
 * call CreateProcessA directly.  Start a console child in the caller's
 * console and wait for it. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fake_dsh_config.h"

#ifndef FAKE_DSH_PYTHON
#error "FAKE_DSH_PYTHON must be defined"
#endif
#ifndef FAKE_DSH_SCRIPT
#error "FAKE_DSH_SCRIPT must be defined"
#endif

#ifdef _WIN32
#include <windows.h>

struct buf {
    char *data;
    size_t len;
    size_t cap;
};

static int reserve(struct buf *b, size_t extra) {
    size_t need = b->len + extra + 1;
    char *grown;
    if (need <= b->cap) return 1;
    if (b->cap == 0) b->cap = 256;
    while (b->cap < need) b->cap *= 2;
    grown = (char *)realloc(b->data, b->cap);
    if (!grown) return 0;
    b->data = grown;
    return 1;
}

static int putc_buf(struct buf *b, char c) {
    if (!reserve(b, 1)) return 0;
    b->data[b->len++] = c;
    b->data[b->len] = '\0';
    return 1;
}

static int puts_buf(struct buf *b, const char *text) {
    size_t n = strlen(text);
    if (!reserve(b, n)) return 0;
    memcpy(b->data + b->len, text, n);
    b->len += n;
    b->data[b->len] = '\0';
    return 1;
}

/* Quote one argv element exactly as the Windows CRT parser expects. */
static int quote_arg(struct buf *b, const char *arg) {
    const char *p;
    size_t backslashes = 0;
    int quote = (*arg == '\0');
    if (!quote) {
        for (p = arg; *p; ++p) {
            if (*p == ' ' || *p == '\t' || *p == '\n' || *p == '"') {
                quote = 1;
                break;
            }
        }
    }
    if (!quote) return puts_buf(b, arg);
    if (!putc_buf(b, '"')) return 0;
    for (p = arg; *p; ++p) {
        if (*p == '\\') {
            ++backslashes;
        } else if (*p == '"') {
            size_t i;
            for (i = 0; i < backslashes * 2 + 1; ++i) {
                if (!putc_buf(b, '\\')) return 0;
            }
            if (!putc_buf(b, '"')) return 0;
            backslashes = 0;
        } else {
            size_t i;
            for (i = 0; i < backslashes; ++i) {
                if (!putc_buf(b, '\\')) return 0;
            }
            backslashes = 0;
            if (!putc_buf(b, *p)) return 0;
        }
    }
    {
        size_t i;
        for (i = 0; i < backslashes * 2; ++i) {
            if (!putc_buf(b, '\\')) return 0;
        }
    }
    return putc_buf(b, '"');
}

int main(int argc, char **argv) {
    struct buf cmd = {0};
    STARTUPINFOA startup;
    PROCESS_INFORMATION info;
    DWORD code = 127;
    int i;
    if (!quote_arg(&cmd, FAKE_DSH_PYTHON)) return 127;
    if (!putc_buf(&cmd, ' ')) { free(cmd.data); return 127; }
    if (!quote_arg(&cmd, FAKE_DSH_SCRIPT)) { free(cmd.data); return 127; }
    for (i = 1; i < argc; ++i) {
        if (!putc_buf(&cmd, ' ') || !quote_arg(&cmd, argv[i])) {
            free(cmd.data);
            return 127;
        }
    }
    memset(&startup, 0, sizeof(startup));
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    memset(&info, 0, sizeof(info));
    if (!CreateProcessA(FAKE_DSH_PYTHON, cmd.data, NULL, NULL, TRUE, 0,
                        NULL, NULL, &startup, &info)) {
        free(cmd.data);
        return 127;
    }
    free(cmd.data);
    WaitForSingleObject(info.hProcess, INFINITE);
    if (GetExitCodeProcess(info.hProcess, &code)) {
        /* keep the real exit status */
    } else {
        code = 127;
    }
    CloseHandle(info.hThread);
    CloseHandle(info.hProcess);
    return (int)code;
}
#else
#include <unistd.h>
int main(int argc, char **argv) {
    int i;
    char **nargv = (char **)calloc((size_t)argc + 2, sizeof(char *));
    if (!nargv) return 127;
    nargv[0] = (char *)FAKE_DSH_PYTHON;
    nargv[1] = (char *)FAKE_DSH_SCRIPT;
    for (i = 1; i < argc; ++i) nargv[i + 1] = argv[i];
    nargv[argc + 1] = NULL;
    execv(FAKE_DSH_PYTHON, nargv);
    perror("execv fake dsh");
    return 127;
}
#endif
