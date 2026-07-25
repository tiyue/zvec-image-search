/* mjson.c - Minimal JSON parser/builder implementation */
#include "mjson.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <math.h>

/* ========================== Parser ========================== */

typedef struct {
    const char *p;
    const char *end;
} parser;

static void skip_ws(parser *ps) {
    while (ps->p < ps->end) {
        char c = *ps->p;
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ps->p++;
        else break;
    }
}

static mj_node *new_node(mj_type type) {
    mj_node *n = (mj_node *)calloc(1, sizeof(mj_node));
    if (n) n->type = type;
    return n;
}

static char *parse_string_raw(parser *ps) {
    if (ps->p >= ps->end || *ps->p != '"') return NULL;
    ps->p++; /* skip opening quote */
    /* First pass: measure length */
    const char *start = ps->p;
    size_t len = 0;
    while (ps->p < ps->end && *ps->p != '"') {
        if (*ps->p == '\\') { ps->p++; len++; }
        else len++;
        ps->p++;
    }
    if (ps->p >= ps->end) return NULL;
    /* Allocate and second pass: copy with escape handling */
    char *out = (char *)malloc(len + 1);
    if (!out) return NULL;
    const char *s = start;
    size_t i = 0;
    while (s < ps->p) {
        if (*s == '\\') {
            s++;
            switch (*s) {
                case '"': out[i++] = '"'; break;
                case '\\': out[i++] = '\\'; break;
                case '/': out[i++] = '/'; break;
                case 'n': out[i++] = '\n'; break;
                case 't': out[i++] = '\t'; break;
                case 'r': out[i++] = '\r'; break;
                case 'b': out[i++] = '\b'; break;
                case 'f': out[i++] = '\f'; break;
                case 'u': {
                    /* Basic BMP handling: copy as UTF-8 */
                    if (s + 4 < ps->end) {
                        char hex[5] = {s[1], s[2], s[3], s[4], 0};
                        unsigned long cp = strtoul(hex, NULL, 16);
                        if (cp < 0x80) {
                            out[i++] = (char)cp;
                        } else if (cp < 0x800) {
                            out[i++] = (char)(0xC0 | (cp >> 6));
                            out[i++] = (char)(0x80 | (cp & 0x3F));
                        } else {
                            out[i++] = (char)(0xE0 | (cp >> 12));
                            out[i++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                            out[i++] = (char)(0x80 | (cp & 0x3F));
                        }
                        s += 4;
                    }
                    break;
                }
                default: out[i++] = *s; break;
            }
            s++;
        } else {
            out[i++] = *s++;
        }
    }
    out[i] = '\0';
    ps->p++; /* skip closing quote */
    return out;
}

static mj_node *parse_value(parser *ps);

static mj_node *parse_object(parser *ps) {
    ps->p++; /* skip '{' */
    mj_node *obj = new_node(MJ_OBJECT);
    if (!obj) return NULL;
    mj_node *tail = NULL;
    skip_ws(ps);
    if (ps->p < ps->end && *ps->p == '}') { ps->p++; return obj; }
    for (;;) {
        skip_ws(ps);
        char *key = parse_string_raw(ps);
        if (!key) { mj_free(obj); return NULL; }
        skip_ws(ps);
        if (ps->p >= ps->end || *ps->p != ':') { free(key); mj_free(obj); return NULL; }
        ps->p++; /* skip ':' */
        mj_node *val = parse_value(ps);
        if (!val) { free(key); mj_free(obj); return NULL; }
        val->key = key;
        if (!obj->child) obj->child = val;
        else tail->next = val;
        tail = val;
        skip_ws(ps);
        if (ps->p < ps->end && *ps->p == ',') { ps->p++; continue; }
        if (ps->p < ps->end && *ps->p == '}') { ps->p++; break; }
        mj_free(obj); return NULL;
    }
    return obj;
}

static mj_node *parse_array(parser *ps) {
    ps->p++; /* skip '[' */
    mj_node *arr = new_node(MJ_ARRAY);
    if (!arr) return NULL;
    mj_node *tail = NULL;
    skip_ws(ps);
    if (ps->p < ps->end && *ps->p == ']') { ps->p++; return arr; }
    for (;;) {
        mj_node *val = parse_value(ps);
        if (!val) { mj_free(arr); return NULL; }
        if (!arr->child) arr->child = val;
        else tail->next = val;
        tail = val;
        skip_ws(ps);
        if (ps->p < ps->end && *ps->p == ',') { ps->p++; continue; }
        if (ps->p < ps->end && *ps->p == ']') { ps->p++; break; }
        mj_free(arr); return NULL;
    }
    return arr;
}

static mj_node *parse_value(parser *ps) {
    skip_ws(ps);
    if (ps->p >= ps->end) return NULL;
    char c = *ps->p;
    if (c == '{') return parse_object(ps);
    if (c == '[') return parse_array(ps);
    if (c == '"') {
        char *s = parse_string_raw(ps);
        if (!s) return NULL;
        mj_node *n = new_node(MJ_STRING);
        if (!n) { free(s); return NULL; }
        n->str_val = s;
        return n;
    }
    if (c == 't' && ps->end - ps->p >= 4 && strncmp(ps->p, "true", 4) == 0) {
        ps->p += 4;
        mj_node *n = new_node(MJ_BOOL);
        if (n) n->bool_val = 1;
        return n;
    }
    if (c == 'f' && ps->end - ps->p >= 5 && strncmp(ps->p, "false", 5) == 0) {
        ps->p += 5;
        mj_node *n = new_node(MJ_BOOL);
        if (n) n->bool_val = 0;
        return n;
    }
    if (c == 'n' && ps->end - ps->p >= 4 && strncmp(ps->p, "null", 4) == 0) {
        ps->p += 4;
        return new_node(MJ_NULL);
    }
    /* number */
    if (c == '-' || (c >= '0' && c <= '9')) {
        char *endp = NULL;
        double v = strtod(ps->p, &endp);
        if (endp == ps->p) return NULL;
        ps->p = endp;
        mj_node *n = new_node(MJ_NUMBER);
        if (n) n->num_val = v;
        return n;
    }
    return NULL;
}

mj_node *mj_parse(const char *json) {
    if (!json) return NULL;
    parser ps = { json, json + strlen(json) };
    mj_node *root = parse_value(&ps);
    return root;
}

void mj_free(mj_node *node) {
    while (node) {
        mj_node *next = node->next;
        if (node->child) mj_free(node->child);
        free(node->key);
        free(node->str_val);
        free(node);
        node = next;
    }
}

/* ========================== Accessors ========================== */

mj_node *mj_get(const mj_node *obj, const char *key) {
    if (!obj || obj->type != MJ_OBJECT || !key) return NULL;
    for (mj_node *c = obj->child; c; c = c->next) {
        if (c->key && strcmp(c->key, key) == 0) return c;
    }
    return NULL;
}

const char *mj_str(const mj_node *obj, const char *key, const char *def) {
    mj_node *n = mj_get(obj, key);
    if (n && n->type == MJ_STRING) return n->str_val;
    return def;
}

double mj_num(const mj_node *obj, const char *key, double def) {
    mj_node *n = mj_get(obj, key);
    if (n && n->type == MJ_NUMBER) return n->num_val;
    return def;
}

int mj_bool(const mj_node *obj, const char *key, int def) {
    mj_node *n = mj_get(obj, key);
    if (n && n->type == MJ_BOOL) return n->bool_val;
    return def;
}

int mj_array_len(const mj_node *arr) {
    if (!arr || arr->type != MJ_ARRAY) return 0;
    int count = 0;
    for (mj_node *c = arr->child; c; c = c->next) count++;
    return count;
}

mj_node *mj_array_at(const mj_node *arr, int index) {
    if (!arr || arr->type != MJ_ARRAY) return NULL;
    mj_node *c = arr->child;
    for (int i = 0; i < index && c; i++) c = c->next;
    return c;
}

/* ========================== Builder ========================== */

static void ensure(mj_builder *b, size_t extra) {
    if (b->len + extra + 1 > b->cap) {
        size_t newcap = (b->cap == 0) ? 256 : b->cap * 2;
        while (newcap < b->len + extra + 1) newcap *= 2;
        b->buf = (char *)realloc(b->buf, newcap);
        b->cap = newcap;
    }
}

static void put(mj_builder *b, const char *s) {
    size_t n = strlen(s);
    ensure(b, n);
    memcpy(b->buf + b->len, s, n);
    b->len += n;
    b->buf[b->len] = '\0';
}

static void put_escaped(mj_builder *b, const char *s) {
    put(b, "\"");
    for (; *s; s++) {
        switch (*s) {
            case '"': put(b, "\\\""); break;
            case '\\': put(b, "\\\\"); break;
            case '\n': put(b, "\\n"); break;
            case '\r': put(b, "\\r"); break;
            case '\t': put(b, "\\t"); break;
            default: {
                ensure(b, 1);
                b->buf[b->len++] = *s;
                b->buf[b->len] = '\0';
                break;
            }
        }
    }
    put(b, "\"");
}

void mj_init(mj_builder *b) { b->buf = NULL; b->len = 0; b->cap = 0; }

void mj_free_builder(mj_builder *b) { free(b->buf); b->buf = NULL; b->len = b->cap = 0; }

char *mj_finish(mj_builder *b) {
    char *r = b->buf ? b->buf : strdup("");
    b->buf = NULL; b->len = b->cap = 0;
    return r;
}

void mj_obj_begin(mj_builder *b) { put(b, "{"); }
void mj_obj_end(mj_builder *b) { put(b, "}"); }
void mj_arr_begin(mj_builder *b) { put(b, "["); }
void mj_arr_end(mj_builder *b) { put(b, "]"); }
void mj_comma(mj_builder *b) { put(b, ","); }

void mj_key(mj_builder *b, const char *key) {
    put_escaped(b, key);
    put(b, ":");
}

void mj_val_str(mj_builder *b, const char *val) { put_escaped(b, val ? val : ""); }

void mj_val_num(mj_builder *b, double val) {
    char tmp[64];
    if (val == (double)(long long)val)
        snprintf(tmp, sizeof(tmp), "%lld", (long long)val);
    else
        snprintf(tmp, sizeof(tmp), "%g", val);
    put(b, tmp);
}

void mj_val_bool(mj_builder *b, int val) { put(b, val ? "true" : "false"); }
void mj_val_null(mj_builder *b) { put(b, "null"); }

void mj_kv_str(mj_builder *b, const char *key, const char *val) {
    mj_key(b, key);
    mj_val_str(b, val);
}

void mj_kv_num(mj_builder *b, const char *key, double val) {
    mj_key(b, key);
    mj_val_num(b, val);
}

void mj_kv_bool(mj_builder *b, const char *key, int val) {
    mj_key(b, key);
    mj_val_bool(b, val);
}

void mj_raw(mj_builder *b, const char *raw) {
    put(b, raw);
}
