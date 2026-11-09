/* Tiny test-only launcher: run the fake dsh Python script with argv intact.
 * Windows CreateProcess cannot execute a shebang script, so unit tests and CI
 * invoke this PE instead.  Paths are baked in by tests/build_fake_dsh.py. */
#include <stdio.h>
#include <stdlib.h>
#include "fake_dsh_config.h"

#ifndef FAKE_DSH_PYTHON
#error "FAKE_DSH_PYTHON must be defined"
#endif
#ifndef FAKE_DSH_SCRIPT
#error "FAKE_DSH_SCRIPT must be defined"
#endif

#ifdef _WIN32
#include <process.h>
int main(int argc, char **argv) {
    int i;
    char **nargv = (char **)calloc((size_t)argc + 2, sizeof(char *));
    if (!nargv) return 127;
    nargv[0] = (char *)FAKE_DSH_PYTHON;
    nargv[1] = (char *)FAKE_DSH_SCRIPT;
    for (i = 1; i < argc; ++i) nargv[i + 1] = argv[i];
    nargv[argc + 1] = NULL;
    {
        intptr_t rc = _spawnv(_P_WAIT, FAKE_DSH_PYTHON,
                              (const char *const *)nargv);
        free(nargv);
        return rc < 0 ? 127 : (int)rc;
    }
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
