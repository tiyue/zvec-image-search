/* mjson.h - Minimal JSON parser/builder for quick_tag.c
 * Supports: objects, arrays, strings, numbers, booleans, null.
 * Not thread-safe. Single allocation per parse tree.
 */
#ifndef MJSON_H
#define MJSON_H

#include <stddef.h>

typedef enum {
    MJ_NULL,
    MJ_BOOL,
    MJ_NUMBER,
    MJ_STRING,
    MJ_ARRAY,
    MJ_OBJECT
} mj_type;

typedef struct mj_node {
    mj_type type;
    char *key;            /* non-NULL when inside an object */
    /* value storage */
    int bool_val;
    double num_val;
    char *str_val;        /* MJ_STRING */
    /* container */
    struct mj_node *child;  /* first child (array/object) */
    struct mj_node *next;   /* next sibling */
} mj_node;

/* Parse a JSON string. Returns root node or NULL on error.
 * The returned tree must be freed with mj_free(). */
mj_node *mj_parse(const char *json);

/* Free a parse tree. */
void mj_free(mj_node *node);

/* Object access: get child by key (non-recursive). */
mj_node *mj_get(const mj_node *obj, const char *key);

/* Convenience getters (return default if missing/wrong type). */
const char *mj_str(const mj_node *obj, const char *key, const char *def);
double mj_num(const mj_node *obj, const char *key, double def);
int mj_bool(const mj_node *obj, const char *key, int def);

/* Array iteration. */
int mj_array_len(const mj_node *arr);
mj_node *mj_array_at(const mj_node *arr, int index);

/* --- Builder API (grows a dynamic string buffer) --- */

typedef struct {
    char *buf;
    size_t len;
    size_t cap;
} mj_builder;

void mj_init(mj_builder *b);
void mj_free_builder(mj_builder *b);
char *mj_finish(mj_builder *b); /* returns owned buffer, resets builder */

void mj_obj_begin(mj_builder *b);
void mj_obj_end(mj_builder *b);
void mj_arr_begin(mj_builder *b);
void mj_arr_end(mj_builder *b);
void mj_key(mj_builder *b, const char *key);
void mj_val_str(mj_builder *b, const char *val);
void mj_val_num(mj_builder *b, double val);
void mj_val_bool(mj_builder *b, int val);
void mj_val_null(mj_builder *b);
void mj_comma(mj_builder *b);

/* Shorthand: key + string value */
void mj_kv_str(mj_builder *b, const char *key, const char *val);
void mj_kv_num(mj_builder *b, const char *key, double val);
void mj_kv_bool(mj_builder *b, const char *key, int val);

/* Append raw JSON text (no escaping). */
void mj_raw(mj_builder *b, const char *raw);

#endif /* MJSON_H */
