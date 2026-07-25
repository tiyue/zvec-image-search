/* quick_tag.c - Batch tag images by parent folder name via Zvec backend API.
 *
 * Compile (MSVC):  cl /O2 /DUNICODE /D_UNICODE quick_tag.c mjson.c ws2_32.lib
 * Compile (MinGW): gcc -O2 -DUNICODE -D_UNICODE -o quick_tag.exe quick_tag.c mjson.c -lws2_32
 *
 * Usage:
 *   quick_tag.exe --gateway "http://127.0.0.1:5635/TOKEN/"
 *   quick_tag.exe --backend-url http://127.0.0.1:5636 --token TOKEN
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include "mjson.h"

#pragma comment(lib, "ws2_32.lib")

/* MinGW compatibility – only needed on older MSVCRT-based MinGW.
 * UCRT-based MinGW-w64 already provides strtok_s, _stricmp, etc. */
#ifndef _MSC_VER
#include <strings.h>
#ifndef _TRUNCATE
static inline char *strtok_s(char *s, const char *d, char **c) { return strtok_r(s, d, c); }
static inline int _stricmp(const char *a, const char *b) { return strcasecmp(a, b); }
static inline int _strnicmp(const char *a, const char *b, size_t n) { return strncasecmp(a, b, n); }
#define _TRUNCATE 0
static inline void strncpy_s(char *d, size_t ds, const char *s, size_t n) {
    (void)n; strncpy(d, s, ds - 1); d[ds - 1] = '\0';
}
static inline void strcat_s(char *d, size_t ds, const char *s) {
    (void)ds; strcat(d, s);
}
#endif /* _TRUNCATE */
#endif /* _MSC_VER */

/* ========================== Constants ========================== */

#define MAX_TAGS 64
#define MAX_TAG_LEN 256
#define MAX_PATH_LEN 4096
#define HTTP_BUF_SIZE (1024 * 1024)
#define POLL_INTERVAL_MS 1000
#define MAX_POLL_MS 300000

#define BLACKLIST_FILE "quick_tag_blacklist.json"
#define MAX_BLACKLIST 256

/* Dynamic blacklist loaded from JSON; falls back to built-in defaults. */
static char **g_blacklist = NULL;
static int g_blacklist_count = 0;

static const char *DEFAULT_BLACKLIST[] = {
    "\xe8\x87\xaa\xe6\x91\x84",   /* 自摄 */
    "\xe8\x87\xaa\xe6\x8b\x8d",   /* 自拍 */
    "\xe8\x87\xaa\xe6\x92\xae",   /* 自撮 */
    "4K", "4k",
    "\xe6\x98\xa0\xe7\x94\xbb",   /* 映画 */
    "Vol", "vol", "VOL",
    "\xe5\x9b\xbe\xe5\x8c\x85",   /* 图包 */
    "\xe5\x8a\xa0\xe5\x86\x95",   /* 加冕 */
    "\xe6\x9c\x88\xe4\xbb\xbd",   /* 月份 */
    "\xe8\x88\xb0\xe9\x95\xbf",   /* 舰长 */
    NULL
};

static const char *IMAGE_EXTS[] = {
    "jpg","jpeg","png","gif","bmp","webp","tiff","tif","svg",
    "ico","heic","heif","avif","raw","cr2","nef","arw","dng","psd", NULL
};

/* ========================== UTF-8 Utilities ========================== */

/* Check if byte starts a CJK character (3-byte UTF-8 in U+4E00-U+9FFF range) */
static int is_cjk_start(const unsigned char *s) {
    if (s[0] >= 0xE4 && s[0] <= 0xE9 && s[1] >= 0x80 && s[1] <= 0xBF)
        return 1;
    /* CJK Extension A: U+3400-U+4DBF */
    if (s[0] == 0xE3 && s[1] >= 0x90 && s[1] <= 0xB6)
        return 1;
    return 0;
}

static int has_cjk(const char *str) {
    const unsigned char *s = (const unsigned char *)str;
    while (*s) {
        if (is_cjk_start(s)) return 1;
        if (*s < 0x80) s++;
        else if (*s < 0xE0) s += 2;
        else if (*s < 0xF0) s += 3;
        else s += 4;
    }
    return 0;
}

static int is_pure_ascii_alnum(const char *str) {
    for (const char *s = str; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c >= 0x80) return 0;
        if (!isalnum(c) && c != '_' && c != '-' && c != '.') return 0;
    }
    return 1;
}

/* Extract all consecutive CJK runs into out (space-separated). Returns count. */
static int extract_chinese(const char *str, char *out, size_t out_size) {
    const unsigned char *s = (const unsigned char *)str;
    size_t oi = 0;
    int in_cjk = 0, count = 0;
    while (*s && oi < out_size - 4) {
        if (is_cjk_start(s)) {
            if (!in_cjk) { if (count > 0 && oi < out_size - 1) out[oi++] = ' '; in_cjk = 1; count++; }
            /* copy 3 bytes */
            out[oi++] = s[0]; out[oi++] = s[1]; out[oi++] = s[2];
            s += 3;
        } else {
            in_cjk = 0;
            if (*s < 0x80) s++;
            else if (*s < 0xE0) s += 2;
            else if (*s < 0xF0) s += 3;
            else s += 4;
        }
    }
    out[oi] = '\0';
    return count;
}

/* ========================== Tag Derivation ========================== */

/* Remove metadata patterns: [35P-417MB], _jpg, brackets, etc. */
static void clean_folder_name(const char *raw, char *out, size_t out_size) {
    size_t oi = 0;
    const char *s = raw;
    while (*s && oi < out_size - 1) {
        /* Skip [digits P - digits unit] pattern */
        if (*s == '[') {
            const char *e = strchr(s, ']');
            if (e) { s = e + 1; continue; }
        }
        /* Skip fullwidth brackets 【...】 (U+3010..U+3011) with content */
        if ((unsigned char)s[0] == 0xE3 && (unsigned char)s[1] == 0x80 &&
            (unsigned char)s[2] == 0x90) {
            /* Find closing 】 (E3 80 91) */
            const char *e = s + 3;
            while (*e) {
                if ((unsigned char)e[0] == 0xE3 && (unsigned char)e[1] == 0x80 &&
                    (unsigned char)e[2] == 0x91) { e += 3; break; }
                e++;
            }
            s = e; continue;
        }
        /* Skip stray fullwidth right bracket 】 */
        if ((unsigned char)s[0] == 0xE3 && (unsigned char)s[1] == 0x80 &&
            (unsigned char)s[2] == 0x91) {
            s += 3; continue;
        }
        out[oi++] = *s++;
    }
    out[oi] = '\0';
    /* Remove trailing _ext pattern (e.g. _jpg, _png) */
    char *last_us = strrchr(out, '_');
    if (last_us && strlen(last_us) <= 5 && is_pure_ascii_alnum(last_us + 1))
        *last_us = '\0';
    /* Trim leading/trailing spaces and dashes */
    char *start = out;
    while (*start == ' ' || *start == '-' || *start == '_') start++;
    char *end = out + strlen(out);
    while (end > start && (end[-1] == ' ' || end[-1] == '-' || end[-1] == '_')) end--;
    *end = '\0';
    if (start != out) memmove(out, start, strlen(start) + 1);
    /* Collapse multiple spaces */
    char *w = out, *r = out;
    int prev_space = 0;
    while (*r) {
        if (*r == ' ') { if (!prev_space) *w++ = ' '; prev_space = 1; }
        else { *w++ = *r; prev_space = 0; }
        r++;
    }
    *w = '\0';
    /* Trim trailing space */
    size_t len = strlen(out);
    if (len > 0 && out[len-1] == ' ') out[len-1] = '\0';
}

/* ========================== Blacklist ========================== */

static void blacklist_add(const char *keyword) {
    if (g_blacklist_count >= MAX_BLACKLIST) return;
    g_blacklist = (char **)realloc(g_blacklist, (g_blacklist_count + 1) * sizeof(char *));
    size_t len = strlen(keyword);
    g_blacklist[g_blacklist_count] = (char *)malloc(len + 1);
    memcpy(g_blacklist[g_blacklist_count], keyword, len + 1);
    g_blacklist_count++;
}

/* Load blacklist from JSON file. Falls back to DEFAULT_BLACKLIST on failure. */
static void load_blacklist(void) {
    FILE *f = fopen(BLACKLIST_FILE, "rb");
    if (!f) {
        /* Fallback to built-in defaults */
        for (int i = 0; DEFAULT_BLACKLIST[i]; i++)
            blacklist_add(DEFAULT_BLACKLIST[i]);
        printf("Blacklist: using built-in defaults (%d keywords).\n", g_blacklist_count);
        return;
    }
    fseek(f, 0, SEEK_END);
    long fsize = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (fsize <= 0 || fsize > 1024 * 1024) {
        fclose(f);
        for (int i = 0; DEFAULT_BLACKLIST[i]; i++)
            blacklist_add(DEFAULT_BLACKLIST[i]);
        printf("Blacklist: file invalid, using built-in defaults.\n");
        return;
    }
    char *buf = (char *)malloc(fsize + 1);
    size_t nread = fread(buf, 1, fsize, f);
    fclose(f);
    buf[nread] = '\0';

    mj_node *root = mj_parse(buf);
    free(buf);
    if (!root) {
        for (int i = 0; DEFAULT_BLACKLIST[i]; i++)
            blacklist_add(DEFAULT_BLACKLIST[i]);
        printf("Blacklist: JSON parse error, using built-in defaults.\n");
        return;
    }
    mj_node *arr = mj_get(root, "blacklist");
    int len = mj_array_len(arr);
    for (int i = 0; i < len; i++) {
        mj_node *item = mj_array_at(arr, i);
        if (item && item->type == MJ_STRING && item->str_val && item->str_val[0])
            blacklist_add(item->str_val);
    }
    mj_free(root);
    if (g_blacklist_count == 0) {
        for (int i = 0; DEFAULT_BLACKLIST[i]; i++)
            blacklist_add(DEFAULT_BLACKLIST[i]);
        printf("Blacklist: empty list in JSON, using built-in defaults.\n");
    } else {
        printf("Blacklist: loaded %d keywords from %s.\n", g_blacklist_count, BLACKLIST_FILE);
    }
}

static void blacklist_free(void) {
    for (int i = 0; i < g_blacklist_count; i++) free(g_blacklist[i]);
    free(g_blacklist);
    g_blacklist = NULL;
    g_blacklist_count = 0;
}

/* Check if segment starts with any blacklist keyword (prefix match) */
static int is_blacklisted(const char *seg) {
    for (int i = 0; i < g_blacklist_count; i++) {
        size_t klen = strlen(g_blacklist[i]);
        if (_strnicmp(seg, g_blacklist[i], klen) == 0) return 1;
    }
    return 0;
}

/* Strip all symbols from a tag: keep only CJK, kana, ASCII alnum. */
static void strip_symbols(char *tag) {
    char *w = tag;
    const unsigned char *r = (const unsigned char *)tag;
    while (*r) {
        if (*r < 0x80) {
            /* ASCII: keep only alnum */
            if (isalnum(*r)) *w++ = *r;
            r++;
        } else if (*r < 0xE0) {
            /* 2-byte: skip (rare symbols) */
            r += 2;
        } else if (*r < 0xF0) {
            /* 3-byte: keep CJK (E4-E9), kana (E3 81-83), fullwidth alnum (EF BC-BD) */
            int keep = 0;
            if (r[0] >= 0xE4 && r[0] <= 0xE9) keep = 1; /* CJK Unified */
            else if (r[0] == 0xE3 && r[1] >= 0x81 && r[1] <= 0x83) keep = 1; /* Hiragana/Katakana */
            else if (r[0] == 0xE3 && r[1] == 0x80 && r[2] == 0x85) keep = 1; /* Katakana middle dot U+3005 */
            if (keep) { *w++ = r[0]; *w++ = r[1]; *w++ = r[2]; }
            r += 3;
        } else {
            r += 4; /* 4-byte: skip */
        }
    }
    *w = '\0';
}

/* Derive tags from a folder name. Returns tag count, fills tags array. */
static int derive_tags(const char *folder_name, char tags[][MAX_TAG_LEN], int max_tags) {
    char cleaned[MAX_PATH_LEN];
    clean_folder_name(folder_name, cleaned, sizeof(cleaned));
    if (cleaned[0] == '\0') return 0;
    /* If pure ASCII alphanumeric (no CJK) → signal to recurse */
    if (!has_cjk(cleaned)) return -1; /* special: need parent */

    int count = 0;
    /* Split by space */
    char *ctx = NULL;
    char *seg = strtok_s(cleaned, " ", &ctx);
    while (seg && count < max_tags) {
        if (is_blacklisted(seg)) { seg = strtok_s(NULL, " ", &ctx); continue; }
        /* Add the segment itself as a tag (strip symbols) */
        strncpy_s(tags[count], MAX_TAG_LEN, seg, _TRUNCATE);
        strip_symbols(tags[count]);
        if (tags[count][0]) count++;
        /* If mixed CJK+ASCII, also extract Chinese-only portion */
        if (has_cjk(seg)) {
            char chinese[MAX_TAG_LEN];
            extract_chinese(seg, chinese, sizeof(chinese));
            strip_symbols(chinese);
            if (chinese[0] && count < max_tags &&
                (count == 0 || strcmp(chinese, tags[count-1]) != 0)) {
                strncpy_s(tags[count], MAX_TAG_LEN, chinese, _TRUNCATE);
                count++;
            }
        }
        seg = strtok_s(NULL, " ", &ctx);
    }
    return count;
}

/* Extract "dirty" tags that the old buggy version incorrectly added.
 * These are metadata portions: [35P-417MB], 【89P-1.64GB】, _jpg, etc.
 * Returns count of dirty tags found. */
static int derive_dirty_tags(const char *folder_name, char tags[][MAX_TAG_LEN], int max_tags) {
    int count = 0;
    const char *s = folder_name;
    while (*s && count < max_tags) {
        /* Extract content inside [...] */
        if (*s == '[') {
            const char *e = strchr(s, ']');
            if (e) {
                char tmp[MAX_TAG_LEN];
                size_t len = (size_t)(e - s - 1);
                if (len >= MAX_TAG_LEN) len = MAX_TAG_LEN - 1;
                memcpy(tmp, s + 1, len); tmp[len] = '\0';
                strip_symbols(tmp);
                if (tmp[0]) { strncpy_s(tags[count], MAX_TAG_LEN, tmp, _TRUNCATE); count++; }
                s = e + 1; continue;
            }
        }
        /* Extract content inside 【...】 */
        if ((unsigned char)s[0] == 0xE3 && (unsigned char)s[1] == 0x80 &&
            (unsigned char)s[2] == 0x90) {
            const char *e = s + 3;
            while (*e) {
                if ((unsigned char)e[0] == 0xE3 && (unsigned char)e[1] == 0x80 &&
                    (unsigned char)e[2] == 0x91) break;
                e++;
            }
            if (*e) {
                char tmp[MAX_TAG_LEN];
                size_t len = (size_t)(e - s - 3);
                if (len >= MAX_TAG_LEN) len = MAX_TAG_LEN - 1;
                memcpy(tmp, s + 3, len); tmp[len] = '\0';
                strip_symbols(tmp);
                if (tmp[0]) { strncpy_s(tags[count], MAX_TAG_LEN, tmp, _TRUNCATE); count++; }
                s = e + 3; continue;
            }
        }
        s++;
    }
    /* Also check for _ext suffix that was added as tag */
    const char *last_us = strrchr(folder_name, '_');
    if (last_us && strlen(last_us) <= 5 && count < max_tags) {
        char tmp[MAX_TAG_LEN];
        strncpy_s(tmp, MAX_TAG_LEN, last_us + 1, _TRUNCATE);
        strip_symbols(tmp);
        if (tmp[0] && is_pure_ascii_alnum(tmp)) {
            strncpy_s(tags[count], MAX_TAG_LEN, tmp, _TRUNCATE); count++;
        }
    }
    return count;
}

/* ========================== HTTP Client ========================== */

typedef struct {
    char host[256];
    int port;
    char token[512];
    int is_gateway;
    char base_path[512]; /* for gateway: includes /TOKEN/ */
} api_config;

static int http_request(const api_config *cfg, const char *method,
                        const char *path, const char *body,
                        char *response, size_t resp_size) {
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);

    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char port_str[16];
    snprintf(port_str, sizeof(port_str), "%d", cfg->port);
    if (getaddrinfo(cfg->host, port_str, &hints, &res) != 0) return -1;

    SOCKET sock = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (sock == INVALID_SOCKET) { freeaddrinfo(res); return -1; }
    if (connect(sock, res->ai_addr, (int)res->ai_addrlen) != 0) {
        closesocket(sock); freeaddrinfo(res); return -1;
    }
    freeaddrinfo(res);

    /* Build request (heap-allocated to avoid stack overflow) */
    char full_path[1024];
    if (cfg->is_gateway)
        snprintf(full_path, sizeof(full_path), "%sapi/%s", cfg->base_path, path);
    else
        snprintf(full_path, sizeof(full_path), "/v1/%s", path);

    size_t body_len = body ? strlen(body) : 0;
    size_t req_cap = 2048 + body_len;
    char *req = (char *)malloc(req_cap);
    if (!req) { closesocket(sock); return -1; }

    if (body) {
        snprintf(req, req_cap,
            "%s %s HTTP/1.1\r\nHost: %s:%d\r\n"
            "Content-Type: application/json\r\nContent-Length: %zu\r\n"
            "%s%s"
            "Connection: close\r\n\r\n%s",
            method, full_path, cfg->host, cfg->port, body_len,
            cfg->token[0] ? "Authorization: Bearer " : "",
            cfg->token[0] ? cfg->token : "",
            body);
        /* Fix: token line needs \r\n before Connection */
        if (cfg->token[0]) {
            snprintf(req, req_cap,
                "%s %s HTTP/1.1\r\nHost: %s:%d\r\n"
                "Content-Type: application/json\r\nContent-Length: %zu\r\n"
                "Authorization: Bearer %s\r\n"
                "Connection: close\r\n\r\n%s",
                method, full_path, cfg->host, cfg->port, body_len, cfg->token, body);
        }
    } else {
        if (cfg->token[0]) {
            snprintf(req, req_cap,
                "%s %s HTTP/1.1\r\nHost: %s:%d\r\n"
                "Authorization: Bearer %s\r\n"
                "Connection: close\r\n\r\n",
                method, full_path, cfg->host, cfg->port, cfg->token);
        } else {
            snprintf(req, req_cap,
                "%s %s HTTP/1.1\r\nHost: %s:%d\r\n"
                "Connection: close\r\n\r\n",
                method, full_path, cfg->host, cfg->port);
        }
    }
    send(sock, req, (int)strlen(req), 0);
    free(req);

    /* Read response */
    size_t total = 0;
    int n;
    while (total < resp_size - 1 &&
           (n = recv(sock, response + total, (int)(resp_size - total - 1), 0)) > 0)
        total += n;
    response[total] = '\0';
    closesocket(sock);

    /* Skip HTTP headers, return body */
    char *body_start = strstr(response, "\r\n\r\n");
    if (!body_start) return -1;
    body_start += 4;
    memmove(response, body_start, strlen(body_start) + 1);
    return 0;
}

/* ========================== API Helpers ========================== */

/* GET request that returns parsed JSON tree. Caller must mj_free. */
static mj_node *api_get(api_config *cfg, const char *path) {
    static char resp[HTTP_BUF_SIZE];
    if (http_request(cfg, "GET", path, NULL, resp, sizeof(resp)) != 0)
        return NULL;
    return mj_parse(resp);
}

/* Submit a job and poll until completion.
 * Gateway mode: POST api/jobs with {"task_type": ..., flat params}
 * Backend mode: POST /v1/jobs with {"command": ..., "params": {...}}
 * On success, *root_out receives the parsed response tree (caller must mj_free).
 * Pass NULL for root_out if the result is not needed. */
static int api_submit_and_poll(api_config *cfg, const char *task_type,
                               const char *params_json, mj_node **root_out) {
    static char resp[HTTP_BUF_SIZE];
    mj_builder b;
    mj_init(&b);
    mj_obj_begin(&b);
    if (cfg->is_gateway) {
        /* Gateway format: flat object with task_type */
        mj_kv_str(&b, "task_type", task_type);
        /* Merge params_json fields (strip outer braces) */
        if (params_json && params_json[0] == '{') {
            const char *inner = params_json + 1;
            size_t ilen = strlen(inner);
            if (ilen > 0 && inner[ilen-1] == '}') ilen--;
            if (ilen > 0) {
                mj_comma(&b);
                /* Write inner content raw */
                char *tmp = (char *)malloc(ilen + 1);
                memcpy(tmp, inner, ilen); tmp[ilen] = '\0';
                mj_raw(&b, tmp);
                free(tmp);
            }
        }
    } else {
        /* Backend format: {"command": ..., "params": {...}} */
        mj_kv_str(&b, "command", task_type);
        mj_comma(&b);
        mj_key(&b, "params");
        mj_raw(&b, params_json);
    }
    mj_obj_end(&b);
    char *body = mj_finish(&b);

    if (http_request(cfg, "POST", "jobs", body, resp, sizeof(resp)) != 0) {
        free(body);
        return -1;
    }
    free(body);

    mj_node *root = mj_parse(resp);
    if (!root) return -1;
    mj_node *job = mj_get(root, "job");
    if (!job) job = root;
    const char *job_id = mj_str(job, "id", NULL);
    if (!job_id) { mj_free(root); return -1; }

    char id_copy[64];
    strncpy_s(id_copy, sizeof(id_copy), job_id, _TRUNCATE);
    mj_free(root);

    /* Poll */
    char poll_path[128];
    snprintf(poll_path, sizeof(poll_path), "jobs/%s", id_copy);
    DWORD start = GetTickCount();
    for (;;) {
        Sleep(POLL_INTERVAL_MS);
        if (http_request(cfg, "GET", poll_path, NULL, resp, sizeof(resp)) != 0)
            return -1;
        root = mj_parse(resp);
        if (!root) return -1;
        job = mj_get(root, "job");
        if (!job) job = root;
        const char *status = mj_str(job, "status", "");
        if (strcmp(status, "succeeded") == 0 || strcmp(status, "partial") == 0 ||
            strcmp(status, "needs_attention") == 0) {
            if (root_out) *root_out = root;
            else mj_free(root);
            return 0;
        }
        if (strcmp(status, "failed") == 0 || strcmp(status, "cancelled") == 0) {
            mj_free(root);
            return -1;
        }
        if (GetTickCount() - start > MAX_POLL_MS) { mj_free(root); return -1; }
    }
}

/* Get libraries. Returns 0 on success, fills image_root and library_id. */
static int api_get_libraries(api_config *cfg, char *image_root, size_t ir_size,
                             char *library_id, size_t lid_size) {
    if (cfg->is_gateway) {
        /* Gateway: GET api/bootstrap */
        mj_node *root = api_get(cfg, "bootstrap");
        if (!root) return -1;
        mj_node *libs = mj_get(root, "libraries");
        if (libs && libs->child) {
            mj_node *first = libs->child;
            const char *ir = mj_str(first, "image_root", NULL);
            const char *lid = mj_str(first, "id", NULL);
            if (ir) strncpy_s(image_root, ir_size, ir, _TRUNCATE);
            if (lid) strncpy_s(library_id, lid_size, lid, _TRUNCATE);
        }
        mj_free(root);
    } else {
        /* Backend: submit "libraries" command */
        mj_node *root = NULL;
        if (api_submit_and_poll(cfg, "libraries", "{}", &root) != 0)
            return -1;
        if (root) {
            mj_node *job = mj_get(root, "job");
            if (!job) job = root;
            mj_node *result = mj_get(job, "result");
            if (result) {
                mj_node *libs = mj_get(result, "libraries");
                if (libs && libs->child) {
                    mj_node *first = libs->child;
                    const char *ir = mj_str(first, "image_root", NULL);
                    const char *lid = mj_str(first, "id", NULL);
                    if (ir) strncpy_s(image_root, ir_size, ir, _TRUNCATE);
                    if (lid) strncpy_s(library_id, lid_size, lid, _TRUNCATE);
                }
            }
            mj_free(root);
        }
    }
    return (image_root[0] && library_id[0]) ? 0 : -1;
}

/* Get folder_key for a relative folder path. Returns 0 on success. */
static int api_get_folder_key(api_config *cfg, const char *library_id,
                              const char *rel_path, char *folder_key, size_t fk_size) {
    /* Extract folder name for query */
    const char *last_slash = strrchr(rel_path, '/');
    const char *folder_name = last_slash ? last_slash + 1 : rel_path;

    /* URL-encode the folder name for the query parameter */
    char encoded_name[2048];
    size_t ei = 0;
    for (const unsigned char *s = (const unsigned char *)folder_name; *s && ei < sizeof(encoded_name) - 4; s++) {
        if (isalnum(*s) || *s == '-' || *s == '_' || *s == '.') {
            encoded_name[ei++] = *s;
        } else {
            snprintf(encoded_name + ei, 4, "%%%02X", *s);
            ei += 3;
        }
    }
    encoded_name[ei] = '\0';

    if (cfg->is_gateway) {
        /* Gateway: GET api/libraries/{id}/folders?query=...&offset=0&limit=500 */
        char path[4096];
        snprintf(path, sizeof(path),
                 "libraries/%s/folders?query=%s&offset=0&limit=500",
                 library_id, encoded_name);
        mj_node *root = api_get(cfg, path);
        if (!root) return -1;
        /* Response may have result.folders or folders directly */
        mj_node *result = mj_get(root, "result");
        mj_node *folders = result ? mj_get(result, "folders") : mj_get(root, "folders");
        int flen = mj_array_len(folders);
        for (int i = 0; i < flen; i++) {
            mj_node *item = mj_array_at(folders, i);
            const char *rf = mj_str(item, "relative_folder", "");
            if (strcmp(rf, rel_path) == 0) {
                const char *fk = mj_str(item, "folder_key", NULL);
                if (fk) strncpy_s(folder_key, fk_size, fk, _TRUNCATE);
                mj_free(root);
                return folder_key[0] ? 0 : -1;
            }
        }
        mj_free(root);
        return -1;
    } else {
        /* Backend: submit folder_list job */
        mj_builder fb;
        mj_init(&fb);
        mj_obj_begin(&fb);
        mj_kv_str(&fb, "library_id", library_id);
        mj_comma(&fb);
        mj_kv_str(&fb, "query", folder_name);
        mj_comma(&fb);
        mj_kv_num(&fb, "offset", 0);
        mj_comma(&fb);
        mj_kv_num(&fb, "limit", 500);
        mj_obj_end(&fb);
        char *params = mj_finish(&fb);

        mj_node *root = NULL;
        if (api_submit_and_poll(cfg, "folder_list", params, &root) != 0) {
            free(params);
            return -1;
        }
        free(params);
        if (root) {
            mj_node *job = mj_get(root, "job");
            if (!job) job = root;
            mj_node *result = mj_get(job, "result");
            if (result) {
                mj_node *folders = mj_get(result, "folders");
                int flen = mj_array_len(folders);
                for (int i = 0; i < flen; i++) {
                    mj_node *item = mj_array_at(folders, i);
                    const char *rf = mj_str(item, "relative_folder", "");
                    if (strcmp(rf, rel_path) == 0) {
                        const char *fk = mj_str(item, "folder_key", NULL);
                        if (fk) strncpy_s(folder_key, fk_size, fk, _TRUNCATE);
                        break;
                    }
                }
            }
            mj_free(root);
        }
        return folder_key[0] ? 0 : -1;
    }
}

/* ========================== File System Scanner ========================== */

/* Convert UTF-8 string to wide string. Returns allocated wchar_t* (caller frees). */
static wchar_t *utf8_to_wide(const char *utf8) {
    int len = MultiByteToWideChar(CP_UTF8, 0, utf8, -1, NULL, 0);
    if (len <= 0) return NULL;
    wchar_t *w = (wchar_t *)malloc(len * sizeof(wchar_t));
    if (w) MultiByteToWideChar(CP_UTF8, 0, utf8, -1, w, len);
    return w;
}

/* Convert wide string to UTF-8. Returns allocated char* (caller frees). */
static char *wide_to_utf8(const wchar_t *wide) {
    int len = WideCharToMultiByte(CP_UTF8, 0, wide, -1, NULL, 0, NULL, NULL);
    if (len <= 0) return NULL;
    char *u = (char *)malloc(len);
    if (u) WideCharToMultiByte(CP_UTF8, 0, wide, -1, u, len, NULL, NULL);
    return u;
}

typedef struct {
    char folder_rel[MAX_PATH_LEN]; /* relative folder path (POSIX-style) */
    char tags[MAX_TAGS][MAX_TAG_LEN];
    int tag_count;
    int status; /* 0=pending, 1=done, -1=error, -2=untagged */
} folder_entry;

static folder_entry *g_folders = NULL;
static int g_folder_count = 0;
static int g_folder_cap = 0;

static void add_folder(const char *rel, const char tags[][MAX_TAG_LEN], int count, int status) {
    if (g_folder_count >= g_folder_cap) {
        g_folder_cap = g_folder_cap ? g_folder_cap * 2 : 256;
        g_folders = (folder_entry *)realloc(g_folders, g_folder_cap * sizeof(folder_entry));
    }
    folder_entry *e = &g_folders[g_folder_count++];
    strncpy_s(e->folder_rel, MAX_PATH_LEN, rel, _TRUNCATE);
    e->tag_count = count;
    e->status = status;
    for (int i = 0; i < count && i < MAX_TAGS; i++)
        strncpy_s(e->tags[i], MAX_TAG_LEN, tags[i], _TRUNCATE);
}

static int is_image_ext(const char *ext) {
    for (int i = 0; IMAGE_EXTS[i]; i++)
        if (_stricmp(ext, IMAGE_EXTS[i]) == 0) return 1;
    return 0;
}

static int is_skip_dir(const char *name) {
    if (name[0] == '.') return 1;
    if (_stricmp(name, "$RECYCLE.BIN") == 0) return 1;
    if (_stricmp(name, "System Volume Information") == 0) return 1;
    if (_stricmp(name, "Thumbs.db") == 0) return 1;
    return 0;
}

/* Convert Windows path to relative POSIX path from root */
static void make_relative(const char *root, const char *full, char *rel, size_t rel_size) {
    size_t rlen = strlen(root);
    const char *p = full;
    if (_strnicmp(p, root, rlen) == 0) p += rlen;
    while (*p == '\\' || *p == '/') p++;
    size_t oi = 0;
    for (; *p && oi < rel_size - 1; p++)
        rel[oi++] = (*p == '\\') ? '/' : *p;
    rel[oi] = '\0';
}

/* Get parent folder name from a relative path */
static void get_parent_folder(const char *rel_path, char *parent, size_t parent_size) {
    /* rel_path like "A/B/C/file.jpg" → parent = "C" */
    char tmp[MAX_PATH_LEN];
    strncpy_s(tmp, sizeof(tmp), rel_path, _TRUNCATE);
    /* Remove filename */
    char *last_slash = strrchr(tmp, '/');
    if (last_slash) *last_slash = '\0';
    else { parent[0] = '\0'; return; }
    /* Get last component */
    char *prev_slash = strrchr(tmp, '/');
    const char *folder = prev_slash ? prev_slash + 1 : tmp;
    strncpy_s(parent, parent_size, folder, _TRUNCATE);
}

/* Get grandparent folder name (one level up) */
static void get_grandparent_folder(const char *rel_path, char *gp, size_t gp_size) {
    char tmp[MAX_PATH_LEN];
    strncpy_s(tmp, sizeof(tmp), rel_path, _TRUNCATE);
    char *last_slash = strrchr(tmp, '/');
    if (last_slash) *last_slash = '\0';
    char *prev_slash = strrchr(tmp, '/');
    if (prev_slash) *prev_slash = '\0';
    char *prev2 = strrchr(tmp, '/');
    const char *folder = prev2 ? prev2 + 1 : tmp;
    strncpy_s(gp, gp_size, folder, _TRUNCATE);
}

/* Recursive scan: find folders containing images, derive tags */
static void scan_directory(const char *root, const char *rel_prefix) {
    /* Build search path in UTF-8, then convert to wide */
    char search_utf8[MAX_PATH_LEN];
    if (rel_prefix[0])
        snprintf(search_utf8, sizeof(search_utf8), "%s/%s/*", root, rel_prefix);
    else
        snprintf(search_utf8, sizeof(search_utf8), "%s/*", root);
    /* Normalize slashes to backslashes for Windows */
    for (char *p = search_utf8; *p; p++) if (*p == '/') *p = '\\';

    wchar_t *search_wide = utf8_to_wide(search_utf8);
    if (!search_wide) return;

    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(search_wide, &fd);
    free(search_wide);
    if (h == INVALID_HANDLE_VALUE) return;

    int has_images = 0;
    do {
        char *name_utf8 = wide_to_utf8(fd.cFileName);
        if (!name_utf8) continue;

        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            if (strcmp(name_utf8, ".") == 0 || strcmp(name_utf8, "..") == 0) {
                free(name_utf8); continue;
            }
            if (is_skip_dir(name_utf8)) { free(name_utf8); continue; }
            char child_rel[MAX_PATH_LEN];
            if (rel_prefix[0])
                snprintf(child_rel, sizeof(child_rel), "%s/%s", rel_prefix, name_utf8);
            else
                snprintf(child_rel, sizeof(child_rel), "%s", name_utf8);
            free(name_utf8);
            scan_directory(root, child_rel);
        } else {
            /* Check if image */
            char *dot = strrchr(name_utf8, '.');
            if (dot && is_image_ext(dot + 1)) has_images = 1;
            free(name_utf8);
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);

    if (!has_images || rel_prefix[0] == '\0') return;

    /* This folder has images - derive tags from folder name */
    char *last_slash = strrchr((char *)rel_prefix, '/');
    const char *folder_name = last_slash ? last_slash + 1 : rel_prefix;

    char tags[MAX_TAGS][MAX_TAG_LEN];
    int tag_count = derive_tags(folder_name, tags, MAX_TAGS);

    if (tag_count == -1 || tag_count == 0) {
        /* Pure ASCII/numbers OR all blacklisted → try parent (one level up) */
        char gp_name[MAX_PATH_LEN];
        get_grandparent_folder(rel_prefix, gp_name, sizeof(gp_name));
        if (gp_name[0]) {
            tag_count = derive_tags(gp_name, tags, MAX_TAGS);
            if (tag_count <= 0) tag_count = 0;
        } else {
            tag_count = 0;
        }
    }

    if (tag_count > 0)
        add_folder(rel_prefix, tags, tag_count, 0);
    else
        add_folder(rel_prefix, tags, 0, -2); /* untagged */
}

/* ========================== Main ========================== */

static void print_usage(void) {
    fprintf(stderr,
        "Usage:\n"
        "  quick_tag.exe --auto\n"
        "  quick_tag.exe --auto --mark-all\n"
        "  quick_tag.exe --gateway \"http://127.0.0.1:5635/TOKEN/\"\n"
        "  quick_tag.exe --backend-url http://127.0.0.1:5636 --token TOKEN\n\n"
        "  --auto       Read gateway URL from %%LOCALAPPDATA%%\\zvec-image-search\\gateway-url.txt\n"
        "  --mark-all   Mark all folders as processed (skip in future runs)\n"
        "  --clean      Remove dirty metadata tags (size/count/symbols)\n");
}

/* ========================== State File ========================== */

#define STATE_FILE "quick_tag_state.txt"
#define LOG_FILE "quick_tag_log.txt"

typedef struct {
    char **paths;
    int count;
    int cap;
} state_set;

static void state_init(state_set *s) { s->paths = NULL; s->count = 0; s->cap = 0; }

static void state_load(state_set *s) {
    FILE *f = fopen(STATE_FILE, "r");
    if (!f) return;
    char line[MAX_PATH_LEN];
    while (fgets(line, sizeof(line), f)) {
        size_t len = strlen(line);
        while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r')) line[--len] = '\0';
        if (len == 0) continue;
        if (s->count >= s->cap) {
            s->cap = s->cap ? s->cap * 2 : 512;
            s->paths = (char **)realloc(s->paths, s->cap * sizeof(char *));
        }
        s->paths[s->count] = (char *)malloc(len + 1);
        memcpy(s->paths[s->count], line, len + 1);
        s->count++;
    }
    fclose(f);
}

static int state_contains(state_set *s, const char *path) {
    for (int i = 0; i < s->count; i++)
        if (strcmp(s->paths[i], path) == 0) return 1;
    return 0;
}

static void state_append(const char *path) {
    FILE *f = fopen(STATE_FILE, "a");
    if (f) { fprintf(f, "%s\n", path); fclose(f); }
}

static void state_free(state_set *s) {
    for (int i = 0; i < s->count; i++) free(s->paths[i]);
    free(s->paths);
    s->paths = NULL; s->count = s->cap = 0;
}

/* Read gateway URL from the auto-discovery file. Returns 1 on success. */
static int read_gateway_url(api_config *cfg) {
    const char *localappdata = getenv("LOCALAPPDATA");
    if (!localappdata) return 0;
    char path[MAX_PATH_LEN];
    snprintf(path, sizeof(path), "%s\\zvec-image-search\\gateway-url.txt", localappdata);
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char url[1024] = {0};
    if (!fgets(url, sizeof(url), f)) { fclose(f); return 0; }
    fclose(f);
    /* Trim trailing newline/whitespace */
    size_t len = strlen(url);
    while (len > 0 && (url[len-1] == '\n' || url[len-1] == '\r' || url[len-1] == ' '))
        url[--len] = '\0';
    if (len == 0) return 0;

    /* Parse URL: http://host:port/TOKEN/ */
    const char *p = strstr(url, "://");
    p = p ? p + 3 : url;
    const char *colon = strchr(p, ':');
    const char *slash = strchr(p, '/');
    if (colon && slash && colon < slash) {
        size_t hlen = (size_t)(colon - p);
        if (hlen >= sizeof(cfg->host)) hlen = sizeof(cfg->host) - 1;
        memcpy(cfg->host, p, hlen); cfg->host[hlen] = '\0';
        cfg->port = atoi(colon + 1);
    } else if (slash) {
        size_t hlen = (size_t)(slash - p);
        if (hlen >= sizeof(cfg->host)) hlen = sizeof(cfg->host) - 1;
        memcpy(cfg->host, p, hlen); cfg->host[hlen] = '\0';
    }
    if (slash) {
        strncpy_s(cfg->base_path, sizeof(cfg->base_path), slash, _TRUNCATE);
        size_t blen = strlen(cfg->base_path);
        if (blen > 0 && cfg->base_path[blen-1] != '/')
            strcat_s(cfg->base_path, sizeof(cfg->base_path), "/");
    }
    cfg->is_gateway = 1;
    return 1;
}

int main(int argc, char *argv[]) {
    api_config cfg = {0};
    cfg.port = 8765;
    strcpy_s(cfg.host, sizeof(cfg.host), "127.0.0.1");

    /* Parse args */
    int auto_mode = 0, mark_all = 0, clean_mode = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--auto") == 0) {
            auto_mode = 1;
        } else if (strcmp(argv[i], "--mark-all") == 0) {
            mark_all = 1;
        } else if (strcmp(argv[i], "--clean") == 0) {
            clean_mode = 1;
        } else if (strcmp(argv[i], "--gateway") == 0 && i + 1 < argc) {
            /* Parse gateway URL: http://host:port/TOKEN/ */
            const char *url = argv[++i];
            const char *p = strstr(url, "://");
            p = p ? p + 3 : url;
            const char *colon = strchr(p, ':');
            const char *slash = strchr(p, '/');
            if (colon && slash && colon < slash) {
                size_t hlen = (size_t)(colon - p);
                strncpy_s(cfg.host, sizeof(cfg.host), p, hlen);
                cfg.port = atoi(colon + 1);
            } else if (slash) {
                size_t hlen = (size_t)(slash - p);
                strncpy_s(cfg.host, sizeof(cfg.host), p, hlen);
            }
            if (slash) {
                strncpy_s(cfg.base_path, sizeof(cfg.base_path), slash, _TRUNCATE);
                /* Ensure trailing slash */
                size_t blen = strlen(cfg.base_path);
                if (blen > 0 && cfg.base_path[blen-1] != '/')
                    strcat_s(cfg.base_path, sizeof(cfg.base_path), "/");
            }
            cfg.is_gateway = 1;
        } else if (strcmp(argv[i], "--backend-url") == 0 && i + 1 < argc) {
            const char *url = argv[++i];
            const char *p = strstr(url, "://");
            p = p ? p + 3 : url;
            const char *colon = strchr(p, ':');
            if (colon) {
                size_t hlen = (size_t)(colon - p);
                strncpy_s(cfg.host, sizeof(cfg.host), p, hlen);
                cfg.port = atoi(colon + 1);
            } else {
                strncpy_s(cfg.host, sizeof(cfg.host), p, _TRUNCATE);
            }
            cfg.is_gateway = 0;
        } else if (strcmp(argv[i], "--token") == 0 && i + 1 < argc) {
            strncpy_s(cfg.token, sizeof(cfg.token), argv[++i], _TRUNCATE);
        } else {
            print_usage();
            return 1;
        }
    }

    if (auto_mode) {
        if (!read_gateway_url(&cfg)) {
            fprintf(stderr,
                "Error: Cannot read gateway URL.\n"
                "  Make sure the Zvec desktop app is running (source version).\n"
                "  Expected file: %%LOCALAPPDATA%%\\zvec-image-search\\gateway-url.txt\n");
            return 1;
        }
        printf("Auto-discovered gateway: %s:%d\n", cfg.host, cfg.port);
    } else if (!cfg.is_gateway && cfg.token[0] == '\0') {
        fprintf(stderr, "Error: --token required for direct backend mode.\n");
        print_usage();
        return 1;
    }

    /* Load blacklist from JSON (or built-in defaults) */
    load_blacklist();

    printf("Connecting to %s:%d (%s mode)...\n", cfg.host, cfg.port,
           cfg.is_gateway ? "gateway" : "backend");

    DWORD start_time = GetTickCount();

    /* Step 1: Get libraries → image_root */
    char image_root[MAX_PATH_LEN] = {0};
    char library_id[256] = {0};
    if (api_get_libraries(&cfg, image_root, sizeof(image_root),
                          library_id, sizeof(library_id)) != 0) {
        fprintf(stderr, "Error: Cannot get libraries from backend.\n");
        return 1;
    }
    printf("Image root: %s\n", image_root);
    printf("Library ID: %s\n", library_id);

    /* Step 2: Scan filesystem */
    printf("Scanning directories...\n");
    scan_directory(image_root, "");
    printf("Found %d folders with images.\n", g_folder_count);

    /* Load state file */
    state_set processed;
    state_init(&processed);
    state_load(&processed);
    if (processed.count > 0)
        printf("State: %d folders already processed (will skip).\n", processed.count);

    /* --mark-all mode: just mark everything and exit */
    if (mark_all) {
        int marked = 0;
        for (int i = 0; i < g_folder_count; i++) {
            if (!state_contains(&processed, g_folders[i].folder_rel)) {
                state_append(g_folders[i].folder_rel);
                marked++;
            }
        }
        printf("\n=== Mark All Done ===\n");
        printf("  Newly marked: %d folders\n", marked);
        printf("  Already marked: %d folders\n", g_folder_count - marked);
        state_free(&processed);
        free(g_folders);
        WSACleanup();
        return 0;
    }

    /* --clean mode: remove dirty metadata tags from all folders */
    if (clean_mode) {
        int cleaned = 0, clean_errors = 0, no_dirty = 0;
        printf("Clean mode: removing dirty metadata tags...\n");
        FILE *f_log = fopen(LOG_FILE, "a");
        if (f_log) {
            SYSTEMTIME st;
            GetLocalTime(&st);
            fprintf(f_log, "\n=== Quick Tag CLEAN: %04d-%02d-%02d %02d:%02d:%02d ===\n",
                    st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
            fprintf(f_log, "Task: Remove dirty metadata tags\n---\n");
        }
        for (int i = 0; i < g_folder_count; i++) {
            folder_entry *e = &g_folders[i];
            /* Get the actual folder name (last component) */
            char *last_slash = strrchr(e->folder_rel, '/');
            const char *fname = last_slash ? last_slash + 1 : e->folder_rel;
            /* Also check parent folder name for dirty tags */
            char parent_name[MAX_PATH_LEN] = {0};
            if (last_slash) {
                char tmp[MAX_PATH_LEN];
                strncpy_s(tmp, MAX_PATH_LEN, e->folder_rel, _TRUNCATE);
                tmp[last_slash - e->folder_rel] = '\0';
                char *ps = strrchr(tmp, '/');
                strncpy_s(parent_name, MAX_PATH_LEN, ps ? ps + 1 : tmp, _TRUNCATE);
            }

            /* Derive dirty tags from folder name and parent */
            char dirty[MAX_TAGS][MAX_TAG_LEN];
            int dirty_count = derive_dirty_tags(fname, dirty, MAX_TAGS);
            if (parent_name[0]) {
                dirty_count += derive_dirty_tags(parent_name, dirty + dirty_count,
                                                 MAX_TAGS - dirty_count);
            }
            if (dirty_count <= 0) { no_dirty++; continue; }

            /* Get folder_key */
            char folder_key[8192] = {0};
            if (api_get_folder_key(&cfg, library_id, e->folder_rel,
                                   folder_key, sizeof(folder_key)) != 0) {
                clean_errors++;
                if (f_log) fprintf(f_log, "[CLEAN-ERROR] %s (folder_key not found)\n", e->folder_rel);
                continue;
            }

            /* Build remove request */
            mj_builder tb;
            mj_init(&tb);
            mj_obj_begin(&tb);
            mj_kv_str(&tb, "library_id", library_id);
            mj_comma(&tb);
            mj_key(&tb, "selection");
            mj_obj_begin(&tb);
            mj_kv_str(&tb, "mode", "folder");
            mj_comma(&tb);
            mj_kv_str(&tb, "folder_key", folder_key);
            mj_comma(&tb);
            mj_kv_bool(&tb, "include_subfolders", 1);
            mj_obj_end(&tb);
            mj_comma(&tb);
            mj_kv_str(&tb, "operation", "remove");
            mj_comma(&tb);
            mj_key(&tb, "tags");
            mj_arr_begin(&tb);
            for (int ti = 0; ti < dirty_count; ti++) {
                if (ti > 0) mj_comma(&tb);
                mj_val_str(&tb, dirty[ti]);
            }
            mj_arr_end(&tb);
            mj_obj_end(&tb);
            char *params = mj_finish(&tb);

            if (api_submit_and_poll(&cfg, "manual_tag_batch", params, NULL) != 0) {
                free(params);
                clean_errors++;
                if (f_log) fprintf(f_log, "[CLEAN-ERROR] %s (remove failed)\n", e->folder_rel);
                continue;
            }
            free(params);
            cleaned++;
            if (cleaned % 50 == 0)
                printf("  Progress: %d folders cleaned...\n", cleaned);
        }
        DWORD elapsed = GetTickCount() - start_time;
        int secs = elapsed / 1000;
        printf("\n=== Clean Done ===\n");
        printf("  Cleaned: %d folders\n", cleaned);
        printf("  No dirty tags: %d folders\n", no_dirty);
        printf("  Errors: %d folders\n", clean_errors);
        printf("  Time: %dm %ds\n", secs / 60, secs % 60);
        if (f_log) {
            fprintf(f_log, "---\nResult: cleaned=%d, no_dirty=%d, errors=%d\n", cleaned, no_dirty, clean_errors);
            fprintf(f_log, "Duration: %dm %ds\n", secs / 60, secs % 60);
            fclose(f_log);
        }
        state_free(&processed);
        free(g_folders);
        blacklist_free();
        WSACleanup();
        return 0;
    }

    int tagged = 0, untagged = 0, errors = 0, skipped = 0;
    FILE *f_log = fopen(LOG_FILE, "a");

    /* Log header */
    if (f_log) {
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(f_log, "\n=== Quick Tag Run: %04d-%02d-%02d %02d:%02d:%02d ===\n",
                st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
        fprintf(f_log, "Task: Batch tag folders by parent folder name\n");
        fprintf(f_log, "Image root: %s\n", image_root);
        fprintf(f_log, "Total folders found: %d\n", g_folder_count);
        fprintf(f_log, "Already processed (skipped): %d\n", processed.count);
        fprintf(f_log, "---\n");
    }

    /* Step 3: For each folder, apply tags */
    for (int i = 0; i < g_folder_count; i++) {
        folder_entry *e = &g_folders[i];

        /* Skip already processed */
        if (state_contains(&processed, e->folder_rel)) {
            skipped++;
            continue;
        }

        if (e->status == -2 || e->tag_count <= 0) {
            untagged++;
            state_append(e->folder_rel);
            if (f_log) fprintf(f_log, "[UNTAGGED] %s\n", e->folder_rel);
            continue;
        }

        /* Get folder_key via API */
        char folder_key[8192] = {0};
        if (api_get_folder_key(&cfg, library_id, e->folder_rel,
                               folder_key, sizeof(folder_key)) != 0) {
            errors++;
            e->status = -1;
            state_append(e->folder_rel);
            if (f_log) fprintf(f_log, "[ERROR] %s (folder_key not found)\n", e->folder_rel);
            continue;
        }

        /* Build manual_tag_batch params */
        mj_builder tb;
        mj_init(&tb);
        mj_obj_begin(&tb);
        mj_kv_str(&tb, "library_id", library_id);
        mj_comma(&tb);
        mj_key(&tb, "selection");
        mj_obj_begin(&tb);
        mj_kv_str(&tb, "mode", "folder");
        mj_comma(&tb);
        mj_kv_str(&tb, "folder_key", folder_key);
        mj_comma(&tb);
        mj_kv_bool(&tb, "include_subfolders", 0);
        mj_obj_end(&tb);
        mj_comma(&tb);
        mj_kv_str(&tb, "operation", "add");
        mj_comma(&tb);
        mj_key(&tb, "tags");
        mj_arr_begin(&tb);
        for (int ti = 0; ti < e->tag_count; ti++) {
            if (ti > 0) mj_comma(&tb);
            mj_val_str(&tb, e->tags[ti]);
        }
        mj_arr_end(&tb);
        mj_obj_end(&tb);
        char *tag_params = mj_finish(&tb);

        if (api_submit_and_poll(&cfg, "manual_tag_batch", tag_params, NULL) != 0) {
            free(tag_params);
            errors++;
            e->status = -1;
            state_append(e->folder_rel);
            if (f_log) fprintf(f_log, "[ERROR] %s (tag batch failed)\n", e->folder_rel);
            continue;
        }
        free(tag_params);
        tagged++;
        e->status = 1;
        state_append(e->folder_rel);

        if (tagged % 50 == 0)
            printf("  Progress: %d folders tagged...\n", tagged);
    }

    DWORD elapsed = GetTickCount() - start_time;
    int secs = elapsed / 1000;

    printf("\n=== Done ===\n");
    printf("  Tagged:   %d folders\n", tagged);
    printf("  Untagged: %d folders\n", untagged);
    printf("  Errors:   %d folders\n", errors);
    printf("  Skipped:  %d folders (already processed)\n", skipped);
    printf("  Time:     %dm %ds\n", secs / 60, secs % 60);

    /* Log summary */
    if (f_log) {
        fprintf(f_log, "---\n");
        fprintf(f_log, "Result: tagged=%d, untagged=%d, errors=%d, skipped=%d\n",
                tagged, untagged, errors, skipped);
        fprintf(f_log, "Duration: %dm %ds\n", secs / 60, secs % 60);
        fclose(f_log);
    }

    state_free(&processed);
    free(g_folders);
    blacklist_free();
    WSACleanup();
    return 0;
}
