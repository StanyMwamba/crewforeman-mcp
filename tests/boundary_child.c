/* Minimal native child for the real Windows ACL boundary test.
 *
 * The boundary exercises file operations under the write-restricted token;
 * it does not need a Python runtime.  Python 3.15.2 exits 0xC0000142 before
 * main under that token on windows-2025 (the status CPython itself treats as
 * a desktop-heap startup failure), so using a native helper keeps the test
 * about the ACL policy instead of about a third-party runtime.  Exit codes:
 * 0 allowed/succeeded, 3 access denied or I/O failure, 4 usage error.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#endif

static int create_file(const char *path, const char *mode, int write_byte) {
    FILE *handle = fopen(path, mode);
    if (handle == NULL) {
        return 3;
    }
    if (write_byte) {
        fputc('x', handle);
    }
    if (fclose(handle) != 0) {
        return 3;
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *op;
    const char *path;
    char *renamed;
    size_t size;

    if (argc < 3) {
        return 4;
    }
    op = argv[1];
    path = argv[2];

    if (strcmp(op, "create") == 0) {
        return create_file(path, "wb", 1);
    }
    if (strcmp(op, "modify") == 0 || strcmp(op, "append") == 0) {
        return create_file(path, "ab", 1);
    }
    if (strcmp(op, "truncate") == 0) {
        return create_file(path, "wb", 0);
    }
    if (strcmp(op, "read") == 0) {
        FILE *handle = fopen(path, "rb");
        if (handle == NULL) {
            return 3;
        }
        (void)fgetc(handle);
        fclose(handle);
        return 0;
    }
    if (strcmp(op, "delete") == 0) {
        return remove(path) == 0 ? 0 : 3;
    }
    if (strcmp(op, "rename") == 0) {
        size = strlen(path) + 8;
        renamed = (char *)malloc(size);
        if (renamed == NULL) {
            return 4;
        }
        snprintf(renamed, size, "%s.moved", path);
#ifdef _WIN32
        if (!MoveFileExA(path, renamed, MOVEFILE_REPLACE_EXISTING)) {
            free(renamed);
            return 3;
        }
#else
        if (rename(path, renamed) != 0) {
            free(renamed);
            return 3;
        }
#endif
        free(renamed);
        return 0;
    }
    return 4;
}
